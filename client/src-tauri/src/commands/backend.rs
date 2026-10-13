//! Backend-download IPC commands — first-run progress state and manual retry.
//!
//! The desktop shell downloads the Python backend on its first launch (see
//! [`crate::services::backend_downloader`]). Progress also arrives as Tauri
//! events; these commands are the pull half so a front end that attached its
//! listeners late still sees the truth, plus the retry behind the failure card.

use crate::services::backend_downloader::{
    ensure_and_launch, BackendDownloadState, BackendDownloadStatus,
};

/// The first-run backend download's current state.
///
/// `state` is `idle` (nothing downloaded — the backend was already installed, or
/// this is a development run) / `running` / `ready` / `failed`.
#[tauri::command]
pub fn backend_download_status(
    state: tauri::State<'_, BackendDownloadState>,
) -> BackendDownloadStatus {
    state.snapshot()
}

/// Retry the backend download after a failure.
///
/// A no-op when the backend is already installed (it is then simply started) or
/// when a download is already in flight.
#[tauri::command]
pub fn retry_backend_download(app: tauri::AppHandle) -> Result<(), String> {
    ensure_and_launch(&app)
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_status_payload_serializes_with_the_frontend_key_names() {
        let status = BackendDownloadStatus {
            state: "running".into(),
            downloaded: 1024,
            total: 4096,
            percent: 25,
            error: None,
        };
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("\"state\":\"running\""));
        assert!(json.contains("\"downloaded\":1024"));
        assert!(json.contains("\"total\":4096"));
        assert!(json.contains("\"percent\":25"));
        assert!(json.contains("\"error\":null"));
    }

    #[test]
    fn a_failure_snapshot_carries_its_message() {
        let status = BackendDownloadStatus {
            state: "failed".into(),
            downloaded: 0,
            total: 0,
            percent: 0,
            error: Some("HTTP 404".into()),
        };
        let round_trip: BackendDownloadStatus =
            serde_json::from_str(&serde_json::to_string(&status).unwrap()).unwrap();
        assert_eq!(round_trip.state, "failed");
        assert_eq!(round_trip.error.as_deref(), Some("HTTP 404"));
    }
}
