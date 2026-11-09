//! Shared download primitives for the optional CUDA pack.
//!
//! The backend itself is bundled into the installer and is never downloaded;
//! only the Linux CUDA pack still uses a GitHub release asset. This module keeps
//! the event/progress shape and version comparison in one place so the pack
//! downloader and the bundled-backend version check agree.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

/// The repository the optional CUDA pack is published in.
pub const GITHUB_REPO: &str = "lasertsole/sherry_agent";

/// One progress report (`total` is 0 while the server sends no length).
#[derive(Debug, Clone, Serialize, Deserialize, TS, PartialEq, Eq)]
#[ts(export, export_to = "../../app/types/backend/download-progress.ts")]
pub struct DownloadProgress {
    /// Bytes received so far (a JSON number — Tauri's IPC has no bigint).
    #[ts(type = "number")]
    pub downloaded: u64,
    /// Expected total size in bytes (0 = unknown).
    #[ts(type = "number")]
    pub total: u64,
    /// Integer percent, 0 while the total is unknown.
    pub percent: u8,
}

impl DownloadProgress {
    pub(crate) fn new(downloaded: u64, total: u64) -> Self {
        let percent = if total > 0 {
            ((downloaded.min(total) * 100) / total) as u8
        } else {
            0
        };
        Self {
            downloaded,
            total,
            percent,
        }
    }
}

/// The event sink a download drives.
///
/// Implemented over Tauri's event system in production and by a recorder in the
/// tests, so the download/extract/swap flow runs without a webview.
pub trait DownloadEvents {
    /// The transfer is starting.
    fn started(&self);
    /// A throttled progress report.
    fn progress(&self, progress: DownloadProgress);
    /// The payload is downloaded and installed.
    fn complete(&self);
    /// The download, extraction or launch failed.
    fn failed(&self, error: &str);
}

/// Compare two versions as plain strings, ignoring a leading `v`.
pub fn versions_match(stored: &str, current: &str) -> bool {
    normalize_version(stored) == normalize_version(current)
}

fn normalize_version(raw: &str) -> &str {
    raw.trim().trim_start_matches(|c| c == 'v' || c == 'V')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_match_across_a_v_prefix() {
        assert!(versions_match("0.1.0", "0.1.0"));
        assert!(versions_match("v0.1.0\n", "0.1.0"));
        assert!(versions_match("V0.2.0", "0.2.0"));
        assert!(!versions_match("0.1.0", "0.2.0"));
        assert!(!versions_match("main", "0.1.0"));
    }

    #[test]
    fn progress_percent_tolerates_an_unknown_total() {
        let progress = DownloadProgress::new(4096, 0);
        assert_eq!(progress.percent, 0);
        let progress = DownloadProgress::new(4096, 2048);
        assert_eq!(progress.percent, 100, "over-reported totals clamp at 100");
    }
}
