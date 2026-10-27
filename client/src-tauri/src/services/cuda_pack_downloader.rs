//! Optional CUDA pack download (packing-plan B3 + C2).
//!
//! The unified installer ships the CPU backend; the CUDA acceleration bits
//! (`nvidia-*` / `triton` / `libtorch_cuda`) are a separate release asset
//! (`sherry-cuda-pack-<platform>-<arch>.tar.gz`, Linux only) the user opts into
//! from the model settings tab — see [`super::cuda_detect`] for the gate.
//!
//! Mirrors [`super::backend_downloader`] on purpose (same staging + swap, same
//! VERSION gate, same event shape) with TWO additions:
//!
//! * **HTTP Range resume (C2)** — a multi-GiB download on a home connection
//!   will be interrupted: an existing partial `.tmp` continues with
//!   `Range: bytes=<n>-`, a server without range support (200) restarts, and a
//!   416 (the tmp is longer than the remote file) resets it.
//! * **`library_dirs()` (D2)** — the directories holding `*.so*` files inside
//!   the pack, which the process manager feeds to the dynamic loader when it
//!   spawns the backend (`ld-linux --library-path`; see
//!   [`super::python_process`]).
//!
//! The pack's `VERSION` records the BACKEND version it was built against
//! (R1): one release pipeline builds both from the same lock, so a matching
//! backend version transitively pins the matching torch ABI.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager};
use ts_rs::TS;

use crate::services::backend_downloader::{
    versions_match, DownloadEvents, DownloadProgress,
};

/// The pack's directory name inside the app home.
pub const PACK_DIR: &str = "sherry-cuda-pack";
/// Partially downloaded archive (next to the pack dir, swept on success).
const DOWNLOAD_TMP: &str = "sherry-cuda-pack-download.tmp";
/// Where an archive is unpacked before it replaces the live pack.
const STAGING_DIR: &str = "sherry-cuda-pack.incoming";
/// Progress throttle (same reasoning as the backend downloader).
const PROGRESS_MIN_INTERVAL: Duration = Duration::from_millis(200);
/// Whole-request cap for a multi-GiB archive on a slow link.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(7200);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);

pub const EVENT_STARTED: &str = "cuda-pack-download-started";
pub const EVENT_PROGRESS: &str = "cuda-pack-download-progress";
pub const EVENT_COMPLETE: &str = "cuda-pack-download-complete";
pub const EVENT_ERROR: &str = "cuda-pack-download-error";

// ── Event payload / state ───────────────────────────────────

/// The CUDA pack download's state, as `cuda_pack_status` reports it.
#[derive(Debug, Clone, Serialize, Deserialize, TS, PartialEq, Eq)]
#[ts(export, export_to = "../../app/types/backend/cuda-pack-status.ts")]
pub struct CudaPackStatus {
    /// `idle` | `running` | `ready` | `failed`.
    pub state: String,
    /// Bytes received so far (a JSON number — Tauri's IPC has no bigint).
    #[ts(type = "number")]
    pub downloaded: u64,
    /// Expected total size in bytes (0 = unknown).
    #[ts(type = "number")]
    pub total: u64,
    /// Integer percent, 0 while the total is unknown.
    pub percent: u8,
    /// Failure message, when `state == "failed"`.
    pub error: Option<String>,
}

impl Default for CudaPackStatus {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            downloaded: 0,
            total: 0,
            percent: 0,
            error: None,
        }
    }
}

/// The shared, thread-safe download state (Tauri-managed).
#[derive(Default)]
pub struct CudaPackDownloadState {
    running: AtomicBool,
    status: Mutex<CudaPackStatus>,
}

impl CudaPackDownloadState {
    /// The current snapshot for the UI.
    pub fn snapshot(&self) -> CudaPackStatus {
        self.status
            .lock()
            .map(|s| s.clone())
            .unwrap_or_else(|_| CudaPackStatus {
                state: "failed".into(),
                error: Some("cuda pack state lock poisoned".into()),
                ..Default::default()
            })
    }

    /// Claim the download slot; `false` when one is already in flight.
    pub fn begin(&self) -> bool {
        if self.running.swap(true, Ordering::SeqCst) {
            return false;
        }
        self.set(CudaPackStatus {
            state: "running".into(),
            ..Default::default()
        });
        true
    }

    /// Release the slot and publish a terminal state.
    pub fn finish(&self, state: &str, error: Option<String>) {
        let mut status = CudaPackStatus {
            state: state.into(),
            error,
            ..Default::default()
        };
        if state == "ready" {
            if let Ok(current) = self.status.lock() {
                status.downloaded = current.downloaded;
                status.total = current.total;
                status.percent = current.percent;
            }
        }
        self.set(status);
        self.running.store(false, Ordering::SeqCst);
    }

    fn progress(&self, progress: DownloadProgress) {
        if let Ok(mut status) = self.status.lock() {
            *status = CudaPackStatus {
                state: "running".into(),
                downloaded: progress.downloaded,
                total: progress.total,
                percent: progress.percent,
                error: None,
            };
        }
    }

    fn set(&self, status: CudaPackStatus) {
        if let Ok(mut guard) = self.status.lock() {
            *guard = status;
        }
    }
}

/// [`DownloadEvents`] over Tauri for the CUDA pack's event names.
pub struct TauriCudaPackEvents {
    app: tauri::AppHandle,
}

impl TauriCudaPackEvents {
    /// Build the sink for one download run.
    pub fn new(app: tauri::AppHandle) -> Self {
        Self { app }
    }
}

impl DownloadEvents for TauriCudaPackEvents {
    fn started(&self) {
        let _ = self.app.emit(EVENT_STARTED, ());
    }

    fn progress(&self, progress: DownloadProgress) {
        if let Some(state) = self.app.try_state::<CudaPackDownloadState>() {
            state.progress(progress.clone());
        }
        let _ = self.app.emit(EVENT_PROGRESS, progress);
    }

    fn complete(&self) {
        let _ = self.app.emit(EVENT_COMPLETE, ());
    }

    fn failed(&self, error: &str) {
        let _ = self.app.emit(EVENT_ERROR, serde_json::json!({ "error": error.to_string() }));
    }
}

// ── The downloader ──────────────────────────────────────────

/// Downloads, unpacks and swaps the CUDA pack inside the app home.
pub struct CudaPackDownloader {
    app_home: PathBuf,
    base_url: String,
}

impl CudaPackDownloader {
    /// A downloader for `app_home`, reading from the repository's latest release.
    pub fn new(app_home: PathBuf) -> Self {
        Self::with_base_url(app_home, default_base_url())
    }

    /// A downloader reading from an explicit base URL (the tests' seam).
    pub fn with_base_url(app_home: PathBuf, base_url: String) -> Self {
        Self { app_home, base_url }
    }

    /// `<app_home>/sherry-cuda-pack/`.
    pub fn pack_dir(&self) -> PathBuf {
        self.app_home.join(PACK_DIR)
    }

    /// The release archive this build downloads (`tar.gz` everywhere: the pack
    /// is Linux-only, and bsdtar/GNU tar both read it).
    pub fn archive_name() -> String {
        "sherry-cuda-pack-linux-x86_64.tar.gz".to_string()
    }

    /// The absolute download URL.
    pub fn download_url(&self) -> String {
        format!(
            "{}/{}",
            self.base_url.trim_end_matches('/'),
            Self::archive_name()
        )
    }

    /// The `VERSION` marker inside the installed pack, when it exists.
    pub fn stored_version(&self) -> Option<String> {
        std::fs::read_to_string(self.pack_dir().join("VERSION"))
            .ok()
            .map(|raw| raw.trim().to_string())
            .filter(|v| !v.is_empty())
    }

    /// Whether the pack is present AND belongs to this backend version.
    ///
    /// A pack with no shared libraries in it is a broken extract, not a pack.
    pub fn pack_ready(&self, backend_version: &str) -> bool {
        let version_ok = self
            .stored_version()
            .is_some_and(|stored| versions_match(&stored, backend_version));
        version_ok && !Self::library_dirs(&self.pack_dir()).is_empty()
    }

    /// Whether this build's platform can use a CUDA pack at all.
    pub fn platform_supported() -> bool {
        cfg!(target_os = "linux")
    }

    /// Stream the archive to `dest` with HTTP Range resume, reporting
    /// throttled progress. A partial `.tmp` continues; a range-hostile or
    /// over-long tmp restarts; the caller owns deleting the tmp on success.
    pub fn download(&self, dest: &Path, events: &dyn DownloadEvents) -> Result<u64, String> {
        let url = self.download_url();
        events.started();
        tracing::info!(%url, dest = %dest.display(), "Downloading the CUDA pack");

        let client = reqwest::blocking::Client::builder()
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(DOWNLOAD_TIMEOUT)
            .build()
            .map_err(|e| format!("failed to build the HTTP client: {e}"))?;

        // C2: continue an interrupted transfer. `existing` is the byte offset;
        // `appended` decides whether the file is opened for append or truncate.
        let mut existing = std::fs::metadata(dest).map(|m| m.len()).unwrap_or(0);
        let mut appended = existing > 0;

        let mut response = loop {
            let mut request = client.get(&url);
            if appended {
                request = request.header("Range", format!("bytes={existing}-"));
            }
            let response = request
                .send()
                .map_err(|e| format!("download failed: {e}"))?;
            let status = response.status().as_u16();
            match status {
                // The server honored the range: append.
                206 if appended => break response,
                // A full answer (no range support, or no tmp): restart.
                200 => {
                    existing = 0;
                    appended = false;
                    break response;
                }
                // The tmp is longer than the remote file: reset and retry once.
                416 if appended => {
                    let _ = std::fs::remove_file(dest);
                    existing = 0;
                    appended = false;
                    continue;
                }
                _ => {
                    return Err(format!("download failed: HTTP {status} for {url}"));
                }
            }
        };

        let content_total = response.content_length().unwrap_or(0);
        // A 206 body is the REMAINDER: the UI's progress counts whole bytes.
        let total = if appended { existing + content_total } else { content_total };
        let mut downloaded = existing;
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(!appended)
            .append(appended)
            .open(dest)
            .map_err(|e| format!("failed to open {}: {e}", dest.display()))?;

        let mut buffer = vec![0u8; 64 * 1024];
        let mut last_emit = Instant::now() - PROGRESS_MIN_INTERVAL;
        loop {
            let read = response
                .read(&mut buffer)
                .map_err(|e| format!("download read error: {e}"))?;
            if read == 0 {
                break;
            }
            file.write_all(&buffer[..read])
                .map_err(|e| format!("download write error: {e}"))?;
            downloaded += read as u64;
            if last_emit.elapsed() >= PROGRESS_MIN_INTERVAL {
                events.progress(DownloadProgress::new(downloaded, total));
                last_emit = Instant::now();
            }
        }
        file.flush()
            .map_err(|e| format!("download flush error: {e}"))?;
        events.progress(DownloadProgress::new(downloaded, total.max(downloaded)));
        tracing::info!(downloaded, total, resumed_from = existing, "CUDA pack archive downloaded");
        Ok(downloaded)
    }

    /// Unpack `archive` into the staging dir, then swap it in as the live pack.
    pub fn install(&self, archive: &Path) -> Result<PathBuf, String> {
        let staging = self.app_home.join(STAGING_DIR);
        let _ = std::fs::remove_dir_all(&staging);
        std::fs::create_dir_all(&staging)
            .map_err(|e| format!("failed to create {}: {e}", staging.display()))?;
        let output = Command::new("tar")
            .arg("xf")
            .arg(archive)
            .arg("-C")
            .arg(&staging)
            .output()
            .map_err(|e| format!("failed to run tar: {e}"))?;
        if !output.status.success() {
            let _ = std::fs::remove_dir_all(&staging);
            return Err(format!(
                "tar failed ({}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }

        // The archive holds the tree's CONTENTS at its root; a pack-wrapped
        // layout is accepted too.
        let staged_root = if staging.join("VERSION").is_file() {
            staging.clone()
        } else {
            staging.join(PACK_DIR)
        };
        if !staged_root.join("VERSION").is_file() || Self::library_dirs(&staged_root).is_empty() {
            let _ = std::fs::remove_dir_all(&staging);
            return Err("the archive is not a valid CUDA pack (no VERSION or no libraries)".into());
        }

        let pack_dir = self.pack_dir();
        let _ = std::fs::remove_dir_all(&pack_dir);
        std::fs::rename(&staged_root, &pack_dir).map_err(|e| {
            format!(
                "failed to move {} into {}: {e}",
                staged_root.display(),
                pack_dir.display()
            )
        })?;
        let _ = std::fs::remove_dir_all(&staging);
        tracing::info!(dir = %pack_dir.display(), "CUDA pack installed");
        Ok(pack_dir)
    }

    /// Download + install in one call. The tmp survives a failure so the next
    /// attempt resumes.
    pub fn ensure(&self, events: &dyn DownloadEvents) -> Result<PathBuf, String> {
        std::fs::create_dir_all(&self.app_home)
            .map_err(|e| format!("failed to create {}: {e}", self.app_home.display()))?;
        let archive = self.app_home.join(DOWNLOAD_TMP);
        let result = self
            .download(&archive, events)
            .and_then(|_| self.install(&archive));
        if result.is_ok() {
            let _ = std::fs::remove_file(&archive);
        }
        result
    }

    /// Remove the installed pack (the "uninstall CUDA pack" action).
    pub fn remove(&self) -> Result<(), String> {
        let dir = self.pack_dir();
        if !dir.exists() {
            return Ok(());
        }
        std::fs::remove_dir_all(&dir).map_err(|e| format!("failed to remove {}: {e}", dir.display()))
    }

    /// Every directory under `pack_dir` that holds shared libraries.
    ///
    /// torch's own loader resolves its CUDA dependencies through the process's
    /// library search path, so the pack's tree layout does not need to mirror
    /// the backend's — every `*.so*`-bearing directory joins the search path
    /// (D2 feeds them to `ld-linux --library-path`, colon-separated).
    pub fn library_dirs(pack_dir: &Path) -> Vec<PathBuf> {
        let mut dirs: Vec<PathBuf> = Vec::new();
        let mut stack = vec![pack_dir.to_path_buf()];
        while let Some(dir) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&dir) else {
                continue;
            };
            let mut has_library = false;
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    stack.push(path);
                } else if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    if name.contains(".so") {
                        has_library = true;
                    }
                }
            }
            if has_library {
                dirs.push(dir);
            }
        }
        dirs.sort();
        dirs
    }
}

fn default_base_url() -> String {
    std::env::var("EMA_BACKEND_URL_BASE").unwrap_or_else(|_| {
        format!(
            "https://github.com/{}/releases/latest/download",
            crate::services::backend_downloader::GITHUB_REPO
        )
    })
}

// ── Tests ─────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn pack_scaffold(home: &Path, version: &str) -> PathBuf {
        let pack = home.join(PACK_DIR);
        std::fs::create_dir_all(pack.join("nvidia/cublas/lib")).unwrap();
        std::fs::write(pack.join("VERSION"), version).unwrap();
        std::fs::write(pack.join("nvidia/cublas/lib/libcublas.so.12"), b"fake").unwrap();
        pack
    }

    #[test]
    fn library_dirs_finds_every_so_bearing_directory() {
        let home = std::env::temp_dir().join("cuda-pack-test-libs");
        let _ = std::fs::remove_dir_all(&home);
        let pack = pack_scaffold(&home, "0.1.3");
        std::fs::create_dir_all(pack.join("torch/lib")).unwrap();
        std::fs::write(pack.join("torch/lib/libtorch_cuda.so"), b"fake").unwrap();
        std::fs::create_dir_all(pack.join("triton/_C")).unwrap();
        std::fs::write(pack.join("triton/_C/libtriton.so"), b"fake").unwrap();
        std::fs::create_dir_all(pack.join("empty")).unwrap();

        let dirs = CudaPackDownloader::library_dirs(&pack);
        let names: Vec<String> = dirs
            .iter()
            .map(|d| d.strip_prefix(&pack).unwrap().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, ["nvidia/cublas/lib", "torch/lib", "triton/_C"]);
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn pack_ready_requires_a_matching_version_and_a_library() {
        let home = std::env::temp_dir().join("cuda-pack-test-ready");
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).unwrap();
        let downloader = CudaPackDownloader::with_base_url(home.clone(), "http://localhost".into());

        assert!(!downloader.pack_ready("0.1.3"), "nothing installed");

        let pack = pack_scaffold(&home, "0.1.3");
        assert!(downloader.pack_ready("0.1.3"));
        assert!(downloader.pack_ready("v0.1.3"), "a leading v is the same version");
        assert!(!downloader.pack_ready("0.1.4"), "a foreign pack must not load");

        std::fs::remove_file(pack.join("nvidia/cublas/lib/libcublas.so.12")).unwrap();
        assert!(!downloader.pack_ready("0.1.3"), "an empty extract is not a pack");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_status_payload_serializes_with_the_frontend_key_names() {
        let json = serde_json::to_string(&CudaPackStatus {
            state: "running".into(),
            downloaded: 1024,
            total: 4096,
            percent: 25,
            error: None,
        })
        .unwrap();
        assert!(json.contains("\"state\":\"running\""));
        assert!(json.contains("\"downloaded\":1024"));
    }

    #[test]
    fn the_pack_is_linux_only() {
        // The release asset only exists for Linux; the other platforms answer
        // "not supported" before any download starts.
        assert_eq!(CudaPackDownloader::platform_supported(), cfg!(target_os = "linux"));
    }
}
