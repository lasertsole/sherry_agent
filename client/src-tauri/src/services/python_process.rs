//! Python backend process lifecycle manager.
//!
//! Spawns the backend as a child process when the Tauri app starts and kills it
//! on exit. Two launch shapes:
//!
//! * **Packaged** — `<app_home>/sherry-backend/sherry-backend`, the PyInstaller
//!   build the shell downloads from the repository's GitHub releases on the
//!   first launch of a packaged app (see [`super::backend_downloader`]). A
//!   packaged app ALWAYS starts this one: the machine has no checkout, no venv
//!   and no `python` to fall back to.
//! * **Checkout** — `python -m server` from the project root. Kept for
//!   `pnpm tauri dev`: a development build neither ships nor downloads the
//!   frozen backend, and starting the checkout server is opt-in through
//!   `EMA_AUTO_START_BACKEND`.
//!
//! Either way the child gets `SHERRY_HOME` pointing at the app-data directory:
//! the backend keeps sessions, memory, logs, skills and its `.env` there
//! (`config/path.py`), because the bundle itself is read-only and gets replaced
//! by the next update.
//!
//! This module uses `std::process::Command` directly — it does not
//! require the tauri-plugin-shell frontend API.

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;

/// Which command starts the backend.
#[derive(Debug, Clone)]
pub enum BackendCommand {
    /// The downloaded PyInstaller executable (absolute path in the app home).
    Packaged(PathBuf),
    /// `python -m server`, run from the checkout.
    Checkout { python: String, root: PathBuf },
}

/// Manages the Python backend subprocess.
///
/// The process is spawned lazily via [`start`](Self::start) and
/// killed automatically when the manager is dropped.
pub struct PythonProcessManager {
    child: Mutex<Option<Child>>,
    command: BackendCommand,
    /// Directory the child runs in AND the value exported as `SHERRY_HOME`.
    app_home: PathBuf,
}

impl PythonProcessManager {
    /// Create a new manager.
    ///
    /// `app_home` is the writable app-data directory: it becomes the child's
    /// working directory and its `SHERRY_HOME` (so the backend writes sessions,
    /// memory, logs, skills and `.env` there instead of into the bundle).
    pub fn new(command: BackendCommand, app_home: PathBuf) -> Self {
        Self {
            child: Mutex::new(None),
            command,
            app_home,
        }
    }

    /// Spawn the backend process. A second call while one is alive is a no-op.
    ///
    /// Returns `Ok(true)` if a new process was spawned,
    /// `Ok(false)` if one was already running.
    pub fn start(&self) -> Result<bool, String> {
        let mut guard = self.child.lock().map_err(|e| format!("lock poisoned: {e}"))?;

        // Check if already running
        if let Some(ref mut child) = *guard {
            match child.try_wait() {
                Ok(None) => {
                    tracing::info!("Python backend already running (pid={})", child.id());
                    return Ok(false);
                }
                Ok(Some(status)) => {
                    tracing::info!("Previous Python backend exited with {status}, restarting");
                }
                Err(e) => {
                    tracing::warn!("Failed to check Python backend status: {e}");
                }
            }
        }

        // The backend creates its tree lazily, but the home itself must exist
        // before the child starts writing into it.
        if let Err(e) = std::fs::create_dir_all(&self.app_home) {
            return Err(format!(
                "Failed to create the app-data directory {}: {e}",
                self.app_home.display()
            ));
        }

        let mut command = match &self.command {
            BackendCommand::Packaged(exe) => {
                tracing::info!(exe = %exe.display(), home = %self.app_home.display(), "Spawning bundled Python backend");
                Command::new(exe)
            }
            BackendCommand::Checkout { python, root } => {
                tracing::info!(python, root = %root.display(), home = %self.app_home.display(), "Spawning checkout Python backend: python -m server");
                let mut c = Command::new(python);
                c.args(["-m", "server"]);
                c
            }
        };

        // The writable root for everything the backend persists. `current_dir`
        // is the same place so a relative path inside the backend can never
        // resolve into the read-only bundle.
        command
            .env("SHERRY_HOME", &self.app_home)
            // Tells the backend it is the packaged desktop app: it then serves
            // even before a model is configured (first run), because refusing
            // would leave a window nobody can configure.
            .env("SHERRY_DESKTOP", "1")
            .current_dir(&self.app_home)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());

        let child = command
            .spawn()
            .map_err(|e| format!("Failed to spawn Python backend: {e}"))?;

        let pid = child.id();
        *guard = Some(child);

        tracing::info!(pid, "Python backend spawned");
        Ok(true)
    }

    /// Kill the Python backend process if running.
    pub fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                tracing::info!(pid = child.id(), "Stopping Python backend");
                let _ = child.kill();
                let _ = child.wait();
                tracing::info!("Python backend stopped");
            }
        }
    }

    /// Check whether the Python backend process is currently alive.
    pub fn is_running(&self) -> bool {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(ref mut child) = *guard {
                return child.try_wait().map(|s| s.is_none()).unwrap_or(false);
            }
        }
        false
    }
}

impl Drop for PythonProcessManager {
    fn drop(&mut self) {
        self.stop();
    }
}

// ── Tests ─────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn checkout(root: &str) -> BackendCommand {
        BackendCommand::Checkout {
            python: "python".into(),
            root: PathBuf::from(root),
        }
    }

    #[test]
    fn manager_starts_with_no_child() {
        let mgr = PythonProcessManager::new(checkout("/tmp"), PathBuf::from("/tmp"));
        assert!(!mgr.is_running());
    }

    #[test]
    fn stop_on_empty_manager_is_safe() {
        let mgr = PythonProcessManager::new(checkout("/tmp"), PathBuf::from("/tmp"));
        mgr.stop(); // Should not panic
    }

    #[test]
    fn start_with_invalid_path_returns_error() {
        let mgr = PythonProcessManager::new(checkout("/nonexistent/path"), PathBuf::from("/tmp"));
        // python -m server will fail because the module doesn't exist there
        let result = mgr.start();
        // Either the spawn itself fails, or the process exits quickly.
        // We just verify it doesn't panic.
        match result {
            Ok(_) => {} // Process spawned but may exit immediately
            Err(msg) => assert!(msg.contains("Failed to spawn")),
        }
        mgr.stop();
    }

    #[test]
    fn a_packaged_command_keeps_its_path() {
        let exe = PathBuf::from("/tmp/sherry-backend/sherry-backend");
        let mgr = PythonProcessManager::new(BackendCommand::Packaged(exe.clone()), PathBuf::from("/tmp"));
        match &mgr.command {
            BackendCommand::Packaged(path) => assert_eq!(path, &exe),
            other => panic!("expected a packaged command, got {other:?}"),
        }
    }

    #[test]
    fn the_child_is_pointed_at_the_app_home() {
        // The writable root travels as SHERRY_HOME and as the working
        // directory, so nothing the backend writes lands in the bundle.
        let home = std::env::temp_dir().join("sherry-home-probe");
        let mgr = PythonProcessManager::new(checkout("/tmp"), home.clone());
        assert_eq!(mgr.app_home, home);
        // start() is the only place that touches the filesystem; the manager
        // itself must not create anything at construction time.
        assert!(!mgr.is_running());
    }
}
