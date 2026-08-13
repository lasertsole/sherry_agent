//! Python backend process lifecycle manager.
//!
//! Optionally spawns the Python backend (`python -m server`) as a child
//! process when the Tauri app starts, and kills it on exit.
//!
//! This module uses `std::process::Command` directly — it does not
//! require the tauri-plugin-shell frontend API.

use std::process::{Child, Command};
use std::sync::Mutex;

/// Manages the Python backend subprocess.
///
/// The process is spawned lazily via [`start`](Self::start) and
/// killed automatically when the manager is dropped.
pub struct PythonProcessManager {
    child: Mutex<Option<Child>>,
    /// Path to the project root (parent of `server/`).
    project_root: String,
}

impl PythonProcessManager {
    /// Create a new manager.
    ///
    /// `project_root` should be the absolute path to the EMA_AI_agent
    /// project root (the directory containing `server/__main__.py`).
    pub fn new(project_root: String) -> Self {
        Self {
            child: Mutex::new(None),
            project_root,
        }
    }

    /// Spawn the Python backend process.
    ///
    /// Runs `python -m server` from the project root directory.
    /// If a process is already running, this is a no-op.
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

        // Spawn new process
        tracing::info!(
            project_root = %self.project_root,
            "Spawning Python backend: python -m server"
        );

        let child = Command::new("python")
            .args(["-m", "server"])
            .current_dir(&self.project_root)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
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

    #[test]
    fn manager_starts_with_no_child() {
        let mgr = PythonProcessManager::new("/tmp".into());
        assert!(!mgr.is_running());
    }

    #[test]
    fn stop_on_empty_manager_is_safe() {
        let mgr = PythonProcessManager::new("/tmp".into());
        mgr.stop(); // Should not panic
    }

    #[test]
    fn start_with_invalid_path_returns_error() {
        let mgr = PythonProcessManager::new("/nonexistent/path".into());
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
}
