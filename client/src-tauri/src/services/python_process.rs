//! Python backend process lifecycle manager.
//!
//! Spawns the backend as a child process when the Tauri app starts and kills it
//! on exit. Two launch shapes:
//!
//! * **Packaged** — the PyInstaller build the shell either ships inside the
//!   bundle (`resource_dir/sherry-backend/`, the unified installer) or downloads
//!   from the repository's GitHub releases on the first launch of a packaged
//!   app (see [`super::backend_downloader`], the transitional fallback). A
//!   packaged app ALWAYS starts one of them: the machine has no checkout, no
//!   venv and no `python` to fall back to.
//! * **Checkout** — `python -m server` from the project root. Kept for
//!   `pnpm tauri dev`: a development build neither ships nor downloads the
//!   frozen backend, and starting the checkout server is opt-in through
//!   `EMA_AUTO_START_BACKEND`.
//!
//! Either way the child gets `--sherry-home <app-data-dir>` on its command
//! line (plus the bare `--sherry-desktop` flag): the backend keeps sessions,
//! memory, logs, skills and its `.env` there (`config/path.py` scans `sys.argv`;
//! the old `SHERRY_HOME`/`SHERRY_DESKTOP` environment variables remain a
//! transitional fallback on the Python side only), because the bundle itself is
//! read-only and gets replaced by the next update.
//!
//! This module uses `std::process::Command` directly — it does not
//! require the tauri-plugin-shell frontend API.

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;

/// Which command starts the backend.
#[derive(Debug, Clone)]
pub enum BackendCommand {
    /// The bundled/downloaded PyInstaller executable, plus the CUDA pack
    /// directory whose libraries join the loader search path when the pack is
    /// present AND its `VERSION` matches the backend tree's (packing-plan
    /// D2/R1; the decision is made per start, so a restart after the download
    /// picks the pack up without rebuilding the manager).
    Packaged {
        exe: PathBuf,
        cuda_pack_dir: Option<PathBuf>,
    },
    /// `python -m server`, run from the checkout.
    Checkout { python: String, root: PathBuf },
}

/// The dynamic loader's path on this platform (`ld-linux`, probed — R2: the
/// location varies by architecture and multiarch layout).
#[cfg(target_os = "linux")]
fn dynamic_loader_path() -> Option<&'static str> {
    [
        "/lib64/ld-linux-x86-64.so.2",
        "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        "/lib/ld-linux-aarch64.so.2",
        "/lib/aarch64-linux-gnu/ld-linux-aarch64.so.2",
    ]
    .into_iter()
    .find(|p| std::path::Path::new(p).is_file())
}

/// Whether the CUDA pack may load into the backend this command would spawn.
///
/// R1 (ABI match): the pack's `VERSION` must equal the backend tree's — one
/// release pipeline builds both from the same lock, so matching backend
/// versions transitively pin the matching torch. Reading them here (per
/// start, not at construction) is what lets `restart_backend` pick up a
/// freshly downloaded pack without rebuilding the manager.
#[cfg(target_os = "linux")]
fn cuda_pack_usable(exe: &Path, pack_dir: &Path) -> bool {
    use crate::services::backend_downloader::versions_match;

    let pack_version = std::fs::read_to_string(pack_dir.join("VERSION")).unwrap_or_default();
    let backend_version = exe
        .parent()
        .and_then(|tree| std::fs::read_to_string(tree.join("VERSION")).ok())
        .unwrap_or_default();
    versions_match(&pack_version, &backend_version)
        && !crate::services::cuda_pack_downloader::CudaPackDownloader::library_dirs(pack_dir)
            .is_empty()
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
    /// working directory and its `--sherry-home` value (so the backend writes
    /// sessions, memory, logs, skills and `.env` there instead of into the
    /// bundle).
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

        let is_packaged = matches!(self.command, BackendCommand::Packaged { .. });
        let command = match &self.command {
            BackendCommand::Packaged { exe, cuda_pack_dir } => {
                tracing::info!(exe = %exe.display(), home = %self.app_home.display(), "Spawning bundled Python backend");
                // D2: the CUDA pack's libraries join the loader's search path
                // via the dynamic loader's own --library-path argument (an
                // inline per-process equivalent of LD_LIBRARY_PATH, no env
                // var). Linux only — no pack is published elsewhere.
                #[cfg(target_os = "linux")]
                if let Some(pack_dir) = cuda_pack_dir {
                    if cuda_pack_usable(exe, pack_dir) {
                        let dirs = crate::services::cuda_pack_downloader::CudaPackDownloader::library_dirs(pack_dir);
                        match dynamic_loader_path() {
                            Some(loader) => {
                                let lib_path = dirs
                                    .iter()
                                    .map(|d| d.as_os_str().to_owned())
                                    .collect::<Vec<_>>()
                                    .join(std::ffi::OsStr::new(":"));
                                tracing::info!(
                                    loader,
                                    dirs = %lib_path.to_string_lossy(),
                                    "Spawning the backend with the CUDA pack's library path"
                                );
                                let mut wrapped = Command::new(loader);
                                wrapped
                                    .arg("--library-path")
                                    .arg(&lib_path)
                                    .arg("--")
                                    .arg(exe);
                                return self.spawn_with(&mut guard, wrapped, is_packaged);
                            }
                            None => {
                                tracing::warn!(
                                    "CUDA pack present but no dynamic loader found; starting without it"
                                );
                            }
                        }
                    }
                }
                Command::new(exe)
            }
            BackendCommand::Checkout { python, root } => {
                tracing::info!(python, root = %root.display(), home = %self.app_home.display(), "Spawning checkout Python backend: python -m server");
                let mut c = Command::new(python);
                c.args(["-m", "server"]);
                c
            }
        };
        self.spawn_with(&mut guard, command, is_packaged)
    }

    /// The shared tail of every launch shape: the home travels as a CLI
    /// argument (config/path.py scans sys.argv — no env var, so nothing leaks
    /// into a spawned terminal or a subprocess scrubber), the working
    /// directory is the same place so a relative path inside the backend can
    /// never resolve into the read-only bundle, and the bare
    /// `--sherry-desktop` flag stays PACKAGED-ONLY, exactly like the old
    /// `SHERRY_DESKTOP=1` env: it makes the backend serve before a model is
    /// configured (first run, because refusing would leave a window nobody
    /// can configure), while a dev checkout must keep refusing so a broken
    /// .env is noticed.
    fn spawn_with(
        &self,
        guard: &mut std::sync::MutexGuard<'_, Option<Child>>,
        mut command: Command,
        packaged: bool,
    ) -> Result<bool, String> {
        command
            .arg("--sherry-home")
            .arg(&self.app_home);
        if packaged {
            command.arg("--sherry-desktop");
        }
        command
            .current_dir(&self.app_home)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());

        let child = command
            .spawn()
            .map_err(|e| format!("Failed to spawn Python backend: {e}"))?;

        let pid = child.id();
        **guard = Some(child);

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
        let mgr = PythonProcessManager::new(
            BackendCommand::Packaged {
                exe: exe.clone(),
                cuda_pack_dir: None,
            },
            PathBuf::from("/tmp"),
        );
        match &mgr.command {
            BackendCommand::Packaged { exe: path, cuda_pack_dir } => {
                assert_eq!(path, &exe);
                assert_eq!(*cuda_pack_dir, None);
            }
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
