//! First-run download of the Python backend from GitHub Releases.
//!
//! The installer ships only the SPA + this shell (~30 MiB). The backend tree is
//! measured in gigabytes, so it lives in the repository's latest GitHub release
//! as a per-platform archive (`sherry-backend-<os>-<arch>.tar.gz|.zip`) and is
//! fetched on the first launch of a release build:
//!
//! ```text
//! launch → <app_home>/sherry-backend/sherry-backend exists AND VERSION matches?
//!   yes → spawn it (every later launch: seconds)
//!   no  → download → extract into a staging dir → swap → spawn
//! ```
//!
//! * **`<app_home>`** is Tauri's app-data directory — the same writable root the
//!   backend gets as `SHERRY_HOME`, so nothing of the user's lives in the
//!   read-only install tree and an update never deletes it.
//! * **Version match** — the archive carries the `VERSION` file
//!   `packaging/build_backend.py` writes (the installer's own version). A
//!   rebuilt installer whose version differs re-downloads; a matching one starts
//!   straight away. A leading `v` is ignored on both sides, so a `v0.1.0` tag
//!   and a `0.1.0` package version are the same version.
//! * **Staging + swap** — the archive unpacks into `sherry-backend.incoming/`
//!   and only replaces `sherry-backend/` once the executable is there, so a
//!   crash mid-extract can never leave a half-tree that passes the readiness
//!   check.
//! * **`EMA_BACKEND_URL_BASE`** overrides the release base URL — the seam that
//!   makes the whole flow testable against a local HTTP server.
//!
//! Progress reaches the UI as Tauri events (`backend-download-started` /
//! `-progress` / `-complete` / `-error`, see the `EVENT_*` constants), and
//! `backend_download_status` answers the same snapshot over IPC for a front end
//! that attached its listeners after the first events fired.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager};
use ts_rs::TS;

use crate::services::python_process::PythonProcessManager;

/// The repository the release archives are published in.
pub const GITHUB_REPO: &str = "lasertsole/sherry_agent";

/// The backend tree's directory name inside the app home.
pub const BACKEND_DIR: &str = "sherry-backend";
/// The version marker inside that tree (`packaging/build_backend.py` writes it).
pub const VERSION_FILE: &str = "VERSION";
/// Partially downloaded archive (next to the backend dir, swept on success).
const DOWNLOAD_TMP: &str = "sherry-backend-download.tmp";
/// Where an archive is unpacked before it replaces the live tree.
const STAGING_DIR: &str = "sherry-backend.incoming";
/// Read size per iteration; also the progress granularity.
const CHUNK_BYTES: usize = 64 * 1024;
/// Progress events are throttled — 64 KiB chunks would emit thousands per second.
const PROGRESS_MIN_INTERVAL: Duration = Duration::from_millis(200);
/// Whole-request cap (a 2 GiB archive on a slow link still fits).
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(3600);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);

pub const EVENT_STARTED: &str = "backend-download-started";
pub const EVENT_PROGRESS: &str = "backend-download-progress";
pub const EVENT_COMPLETE: &str = "backend-download-complete";
pub const EVENT_ERROR: &str = "backend-download-error";

// ── Platform naming ─────────────────────────────────────────

/// The backend executable's file name on this platform.
pub fn backend_exe_name() -> &'static str {
    if cfg!(windows) {
        "sherry-backend.exe"
    } else {
        "sherry-backend"
    }
}

/// `(platform, arch, archive extension)` of the running build.
pub fn platform_tags() -> (&'static str, &'static str, &'static str) {
    if cfg!(target_os = "windows") {
        ("windows", "x86_64", "zip")
    } else if cfg!(target_os = "macos") {
        ("macos", "arm64", "tar.gz")
    } else {
        ("linux", "x86_64", "tar.gz")
    }
}

/// The release archive's file name for one platform triple.
pub fn archive_name(platform: &str, arch: &str, extension: &str) -> String {
    format!("sherry-backend-{platform}-{arch}.{extension}")
}

/// Compare two versions as plain strings, ignoring a leading `v`.
pub fn versions_match(stored: &str, current: &str) -> bool {
    normalize_version(stored) == normalize_version(current)
}

fn normalize_version(raw: &str) -> &str {
    raw.trim().trim_start_matches(|c| c == 'v' || c == 'V')
}

fn default_base_url() -> String {
    std::env::var("EMA_BACKEND_URL_BASE").unwrap_or_else(|_| {
        format!("https://github.com/{GITHUB_REPO}/releases/latest/download")
    })
}

// ── Event payloads ──────────────────────────────────────────

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

/// The download's state, as `backend_download_status` reports it.
///
/// `state` is one of `idle` / `running` / `ready` / `failed`.
#[derive(Debug, Clone, Serialize, Deserialize, TS, PartialEq, Eq)]
#[ts(export, export_to = "../../app/types/backend/backend-download-status.ts")]
pub struct BackendDownloadStatus {
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

impl Default for BackendDownloadStatus {
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

/// The event sink the download drives.
///
/// Implemented over Tauri's event system in production and by a recorder in the
/// tests, so the whole download/extract/swap flow runs without a webview.
pub trait DownloadEvents {
    /// The transfer is starting (the UI shows its progress card).
    fn started(&self);
    /// A throttled progress report.
    fn progress(&self, progress: DownloadProgress);
    /// The backend is downloaded, installed AND running.
    fn complete(&self);
    /// The download, the extraction or the launch failed.
    fn failed(&self, error: &str);
}

/// The shared, thread-safe download state (Tauri-managed).
#[derive(Default)]
pub struct BackendDownloadState {
    running: AtomicBool,
    status: Mutex<BackendDownloadStatus>,
}

impl BackendDownloadState {
    /// The current snapshot for the UI.
    pub fn snapshot(&self) -> BackendDownloadStatus {
        self.status
            .lock()
            .map(|s| s.clone())
            .unwrap_or_else(|_| BackendDownloadStatus {
                state: "failed".into(),
                error: Some("download state lock poisoned".into()),
                ..Default::default()
            })
    }

    /// Claim the download slot; `false` when one is already in flight.
    pub fn begin(&self) -> bool {
        if self.running.swap(true, Ordering::SeqCst) {
            return false;
        }
        self.set(BackendDownloadStatus {
            state: "running".into(),
            ..Default::default()
        });
        true
    }

    /// Release the slot and publish a terminal state.
    pub fn finish(&self, state: &str, error: Option<String>) {
        let mut status = BackendDownloadStatus {
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
            *status = BackendDownloadStatus {
                state: "running".into(),
                downloaded: progress.downloaded,
                total: progress.total,
                percent: progress.percent,
                error: None,
            };
        }
    }

    fn set(&self, status: BackendDownloadStatus) {
        if let Ok(mut guard) = self.status.lock() {
            *guard = status;
        }
    }
}

/// [`DownloadEvents`] over Tauri: emits the frames and mirrors the state.
pub struct TauriDownloadEvents {
    app: tauri::AppHandle,
}

impl TauriDownloadEvents {
    /// Build the sink for one download run.
    pub fn new(app: tauri::AppHandle) -> Self {
        Self { app }
    }
}

impl DownloadEvents for TauriDownloadEvents {
    fn started(&self) {
        let _ = self.app.emit(EVENT_STARTED, ());
    }

    fn progress(&self, progress: DownloadProgress) {
        if let Some(state) = self.app.try_state::<BackendDownloadState>() {
            state.progress(progress.clone());
        }
        let _ = self.app.emit(EVENT_PROGRESS, progress);
    }

    fn complete(&self) {
        let _ = self.app.emit(EVENT_COMPLETE, ());
    }

    fn failed(&self, error: &str) {
        let _ = self.app.emit(
            EVENT_ERROR,
            serde_json::json!({ "error": error.to_string() }),
        );
    }
}

// ── The downloader ──────────────────────────────────────────

/// Downloads, unpacks and swaps the backend tree inside the app home.
pub struct BackendDownloader {
    app_home: PathBuf,
    base_url: String,
}

impl BackendDownloader {
    /// A downloader for `app_home`, reading from the repository's latest release.
    pub fn new(app_home: PathBuf) -> Self {
        Self::with_base_url(app_home, default_base_url())
    }

    /// A downloader reading from an explicit base URL (the tests' seam).
    pub fn with_base_url(app_home: PathBuf, base_url: String) -> Self {
        Self { app_home, base_url }
    }

    /// `<app_home>/sherry-backend/`.
    pub fn backend_dir(&self) -> PathBuf {
        self.app_home.join(BACKEND_DIR)
    }

    /// `<app_home>/sherry-backend/sherry-backend[.exe]`.
    pub fn backend_path(&self) -> PathBuf {
        self.backend_dir().join(backend_exe_name())
    }

    /// The release archive this build downloads.
    pub fn archive_name(&self) -> String {
        let (platform, arch, extension) = platform_tags();
        archive_name(platform, arch, extension)
    }

    /// The absolute download URL.
    pub fn download_url(&self) -> String {
        format!("{}/{}", self.base_url.trim_end_matches('/'), self.archive_name())
    }

    /// The `VERSION` marker inside the installed tree, when it exists.
    pub fn stored_version(&self) -> Option<String> {
        std::fs::read_to_string(self.backend_dir().join(VERSION_FILE))
            .ok()
            .map(|raw| normalize_version(&raw).to_string())
            .filter(|v| !v.is_empty())
    }

    /// Whether the installed tree belongs to a different app version.
    pub fn needs_update(&self, current_version: &str) -> bool {
        match self.stored_version() {
            Some(stored) => !versions_match(&stored, current_version),
            None => true,
        }
    }

    /// Whether the tree is present AND belongs to this app version.
    pub fn backend_ready(&self, current_version: &str) -> bool {
        self.backend_path().is_file() && !self.needs_update(current_version)
    }

    /// Stream the archive to `dest`, reporting throttled progress.
    pub fn download(&self, dest: &Path, events: &dyn DownloadEvents) -> Result<u64, String> {
        let url = self.download_url();
        events.started();
        tracing::info!(%url, dest = %dest.display(), "Downloading the Python backend");

        let client = reqwest::blocking::Client::builder()
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(DOWNLOAD_TIMEOUT)
            .build()
            .map_err(|e| format!("failed to build the HTTP client: {e}"))?;
        let mut response = client
            .get(&url)
            .send()
            .map_err(|e| format!("download failed: {e}"))?;
        if !response.status().is_success() {
            return Err(format!(
                "download failed: HTTP {} for {url}",
                response.status().as_u16()
            ));
        }

        let total = response.content_length().unwrap_or(0);
        let mut file = std::fs::File::create(dest)
            .map_err(|e| format!("failed to create {}: {e}", dest.display()))?;

        let mut buffer = vec![0u8; CHUNK_BYTES];
        let mut downloaded: u64 = 0;
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
        tracing::info!(downloaded, total, "Backend archive downloaded");
        Ok(downloaded)
    }


    ///
    /// One command shape for all three platforms: GNU tar (Linux) auto-detects
    /// the gzip stream on read, bsdtar (macOS, and Windows 10+'s always-present
    /// `tar.exe`) additionally reads zip — so no unpacking crates are needed.
    pub fn extract(&self, archive: &Path, dest: &Path) -> Result<(), String> {
        let output = Command::new("tar")
            .arg("xf")
            .arg(archive)
            .arg("-C")
            .arg(dest)
            .output()
            .map_err(|e| format!("failed to run tar: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "tar failed ({}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(())
    }

    /// Unpack `archive` into the staging dir, then swap it in as the live tree.
    pub fn install(&self, archive: &Path) -> Result<PathBuf, String> {
        let staging = self.app_home.join(STAGING_DIR);
        let _ = std::fs::remove_dir_all(&staging);
        std::fs::create_dir_all(&staging)
            .map_err(|e| format!("failed to create {}: {e}", staging.display()))?;
        self.extract(archive, &staging)?;

        // The archive holds the tree's CONTENTS at its root (`cd sherry-backend
        // && tar czf … .`); a `sherry-backend/`-wrapped archive is accepted too.
        let staged_root = if staging.join(backend_exe_name()).is_file() {
            staging.clone()
        } else {
            staging.join(BACKEND_DIR)
        };
        let staged_exe = staged_root.join(backend_exe_name());
        if !staged_exe.is_file() {
            let _ = std::fs::remove_dir_all(&staging);
            return Err(format!(
                "the archive does not contain {}",
                backend_exe_name()
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&staged_exe, std::fs::Permissions::from_mode(0o755));
        }

        // Swap only once a complete tree exists in staging: a crash before this
        // point leaves the old tree (or nothing) behind, never a half one.
        let backend_dir = self.backend_dir();
        let _ = std::fs::remove_dir_all(&backend_dir);
        std::fs::rename(&staged_root, &backend_dir).map_err(|e| {
            format!(
                "failed to move {} into {}: {e}",
                staged_root.display(),
                backend_dir.display()
            )
        })?;
        // A wrapper layout leaves the (now empty) staging dir behind.
        let _ = std::fs::remove_dir_all(&staging);
        tracing::info!(dir = %backend_dir.display(), "Python backend installed");
        Ok(self.backend_path())
    }

    /// Download + install in one call. Fails before touching the live tree.
    pub fn ensure(&self, events: &dyn DownloadEvents) -> Result<PathBuf, String> {
        std::fs::create_dir_all(&self.app_home)
            .map_err(|e| format!("failed to create {}: {e}", self.app_home.display()))?;
        let archive = self.app_home.join(DOWNLOAD_TMP);
        let result = self
            .download(&archive, events)
            .and_then(|_| self.install(&archive));
        let _ = std::fs::remove_file(&archive);
        result
    }
}

// ── First-run orchestration ─────────────────────────────────

/// Ensure the backend for this app version is installed, then spawn it.
///
/// Fast path: a tree whose `VERSION` matches this build starts straight away and
/// no event is emitted (the user sees an ordinary launch). Otherwise the
/// download runs on a background thread — the window is already up and shows the
/// progress card — and the backend is spawned from that thread once the tree is
/// installed, so `backend-download-complete` really means "backend running".
///
/// A development build has no downloaded backend to manage (`lib.rs` never
/// builds the packaged manager there, and `pnpm tauri dev` runs the checkout
/// server), so this refuses instead of downloading a gigabyte nobody will use.
pub fn ensure_and_launch(app: &tauri::AppHandle) -> Result<(), String> {
    if cfg!(dev) {
        return Err("this is a development build: it runs the checkout backend, not a downloaded one"
            .into());
    }
    let app_home = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app-data directory: {e}"))?;
    let downloader = BackendDownloader::new(app_home);
    let version = app.package_info().version.to_string();

    if downloader.backend_ready(&version) {
        tracing::info!(
            %version,
            exe = %downloader.backend_path().display(),
            "Python backend present; starting it"
        );
        return spawn_backend(app);
    }

    if !app.state::<BackendDownloadState>().begin() {
        tracing::info!("a backend download is already in flight; not starting another");
        return Ok(());
    }
    tracing::info!(
        %version,
        url = %downloader.download_url(),
        "Python backend missing or stale; downloading it in the background"
    );
    let handle = app.clone();
    std::thread::spawn(move || run_download(handle, downloader));
    Ok(())
}

/// The background half: download → install → spawn → events.
fn run_download(app: tauri::AppHandle, downloader: BackendDownloader) {
    let events = TauriDownloadEvents::new(app.clone());
    match downloader.ensure(&events).and_then(|_| spawn_backend(&app)) {
        Ok(()) => {
            app.state::<BackendDownloadState>().finish("ready", None);
            events.complete();
        }
        Err(error) => {
            tracing::warn!("Python backend download failed: {error}");
            app.state::<BackendDownloadState>()
                .finish("failed", Some(error.clone()));
            events.failed(&error);
        }
    }
}

/// Start the managed backend, treating "already running" as success.
fn spawn_backend(app: &tauri::AppHandle) -> Result<(), String> {
    let manager = app
        .try_state::<PythonProcessManager>()
        .ok_or_else(|| "the backend manager is not registered (a development run?)".to_string())?;
    manager
        .start()
        .map(|_| ())
        .map_err(|e| format!("failed to start the Python backend: {e}"))
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::Mutex as StdMutex;

    /// A [`DownloadEvents`] recorder — the whole flow runs without a webview.
    #[derive(Default)]
    struct Recorder {
        events: StdMutex<Vec<String>>,
    }

    impl Recorder {
        fn log(&self, line: String) {
            self.events.lock().unwrap().push(line);
        }
        fn lines(&self) -> Vec<String> {
            self.events.lock().unwrap().clone()
        }
    }

    impl DownloadEvents for Recorder {
        fn started(&self) {
            self.log("started".into());
        }
        fn progress(&self, progress: DownloadProgress) {
            self.log(format!("progress {} {}", progress.downloaded, progress.percent));
        }
        fn complete(&self) {
            self.log("complete".into());
        }
        fn failed(&self, error: &str) {
            self.log(format!("failed {error}"));
        }
    }

    fn walkdir(dir: &Path, prefix: &str) -> Vec<String> {
        let mut out = Vec::new();
        if let Ok(rd) = std::fs::read_dir(dir) {
            for entry in rd.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                let path = entry.path();
                let kind = if path.is_dir() { "dir" } else { "file" };
                out.push(format!("{prefix}{name}({kind})"));
                if path.is_dir() {
                    out.extend(walkdir(&path, &format!("{prefix}{name}/")));
                }
            }
        }
        out
    }

    fn temp_home(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "sherry-downloader-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Build an archive with the shape the workflow ships: a `cd sherry-backend &&
    /// tar czf … .` tarball, i.e. the tree's CONTENTS at the archive root.
    fn build_archive(home: &Path, payload: &str) -> PathBuf {
        build_archive_of(home, &home.join("payload").join(BACKEND_DIR), payload)
    }

    /// Build an archive whose root is `tree`, holding the exe + a VERSION marker.
    fn build_archive_of(home: &Path, tree: &Path, payload: &str) -> PathBuf {
        std::fs::create_dir_all(tree).unwrap();
        std::fs::write(tree.join(backend_exe_name()), payload).unwrap();
        std::fs::write(tree.join(VERSION_FILE), "0.1.0\n").unwrap();
        let archive = home.join(format!(
            "{}.tar.gz",
            tree.file_name().unwrap().to_string_lossy()
        ));
        let status = Command::new("tar")
            .arg("czf")
            .arg(&archive)
            .arg("-C")
            .arg(tree)
            .arg(".")
            .status()
            .unwrap();
        assert!(status.success());
        archive
    }

    /// Serve `body` once on a loopback port; returns the base URL.
    fn serve_once(body: Vec<u8>, status_line: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut request = [0u8; 1024];
                let _ = std::io::Read::read(&mut stream, &mut request);
                let header = format!(
                    "HTTP/1.1 {status_line}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(header.as_bytes());
                let _ = stream.write_all(&body);
            }
        });
        format!("http://127.0.0.1:{port}")
    }

    #[test]
    fn archive_names_are_platform_tagged() {
        assert_eq!(
            archive_name("linux", "x86_64", "tar.gz"),
            "sherry-backend-linux-x86_64.tar.gz"
        );
        assert_eq!(
            archive_name("windows", "x86_64", "zip"),
            "sherry-backend-windows-x86_64.zip"
        );
        assert_eq!(
            archive_name("macos", "arm64", "tar.gz"),
            "sherry-backend-macos-arm64.tar.gz"
        );
        // The running build resolves to one of them.
        let (platform, arch, extension) = platform_tags();
        assert!(matches!(platform, "linux" | "macos" | "windows"));
        assert!(!arch.is_empty());
        assert!(matches!(extension, "tar.gz" | "zip"));
    }

    #[test]
    fn the_download_url_follows_the_base_url() {
        let downloader = BackendDownloader::with_base_url(
            PathBuf::from("/tmp"),
            "https://example.test/releases/latest/download/".into(),
        );
        assert_eq!(
            downloader.download_url(),
            format!(
                "https://example.test/releases/latest/download/{}",
                downloader.archive_name()
            )
        );
    }

    #[test]
    fn the_default_base_url_points_at_the_release() {
        let downloader = BackendDownloader::with_base_url(
            PathBuf::from("/tmp"),
            default_base_url(),
        );
        assert!(downloader.download_url().contains(GITHUB_REPO));
        assert!(downloader.download_url().contains("/releases/latest/download/"));
    }

    #[test]
    fn versions_match_across_a_v_prefix() {
        assert!(versions_match("0.1.0", "0.1.0"));
        assert!(versions_match("v0.1.0\n", "0.1.0"));
        assert!(versions_match("V0.2.0", "0.2.0"));
        assert!(!versions_match("0.1.0", "0.2.0"));
        assert!(!versions_match("main", "0.1.0"));
    }

    #[test]
    fn readiness_requires_the_exe_and_a_matching_version() {
        let home = temp_home("ready");
        let downloader = BackendDownloader::with_base_url(home.clone(), "http://x".into());
        assert!(!downloader.backend_ready("0.1.0"));

        std::fs::create_dir_all(downloader.backend_dir()).unwrap();
        std::fs::write(downloader.backend_path(), b"#!/bin/sh\n").unwrap();
        // Exe without a VERSION marker → still an update.
        assert!(downloader.needs_update("0.1.0"));
        assert!(!downloader.backend_ready("0.1.0"));

        std::fs::write(downloader.backend_dir().join(VERSION_FILE), "0.1.0\n").unwrap();
        assert!(downloader.backend_ready("0.1.0"));
        // A rebuilt installer (new version) refreshes the tree.
        assert!(!downloader.backend_ready("0.2.0"));
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn a_download_streams_to_disk_and_reports_progress() {
        let home = temp_home("download");
        let payload = vec![b'x'; CHUNK_BYTES * 3 + 17];
        let base = serve_once(payload.clone(), "200 OK");
        let downloader = BackendDownloader::with_base_url(home.clone(), base);
        let recorder = Recorder::default();

        let dest = home.join("archive.bin");
        let written = downloader.download(&dest, &recorder).unwrap();

        assert_eq!(written, payload.len() as u64);
        assert_eq!(std::fs::read(&dest).unwrap(), payload);
        let lines = recorder.lines();
        assert_eq!(lines.first().unwrap(), "started");
        assert!(lines.iter().any(|l| l.starts_with("progress ")), "{lines:?}");
        // The last report carries the full size so the bar lands on 100%.
        assert_eq!(
            lines.last().unwrap(),
            &format!("progress {} 100", payload.len())
        );
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn an_http_error_fails_without_writing_a_tree() {
        let home = temp_home("http-error");
        let base = serve_once(b"not found".to_vec(), "404 Not Found");
        let downloader = BackendDownloader::with_base_url(home.clone(), base);
        let recorder = Recorder::default();

        let error = downloader
            .ensure(&recorder)
            .expect_err("a 404 must fail");
        assert!(error.contains("404"), "{error}");
        assert!(!downloader.backend_dir().exists());
        assert!(!home.join(DOWNLOAD_TMP).exists(), "the temp archive is swept");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn ensure_installs_the_tree_and_reports_completion() {
        let home = temp_home("ensure");
        let archive = build_archive(&home, "#!/bin/sh\necho backend\n");
        let bytes = std::fs::read(&archive).unwrap();
        let base = serve_once(bytes, "200 OK");
        let downloader = BackendDownloader::with_base_url(home.clone(), base);
        let recorder = Recorder::default();

        let exe = downloader.ensure(&recorder).unwrap();

        assert_eq!(exe, downloader.backend_path());
        assert!(exe.is_file());
        assert_eq!(downloader.stored_version().as_deref(), Some("0.1.0"));
        assert!(downloader.backend_ready("0.1.0"));
        assert!(!home.join(STAGING_DIR).exists(), "staging is consumed");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&exe).unwrap().permissions().mode();
            assert_eq!(mode & 0o111, 0o111, "the executable bit is set");
        }
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn a_wrapped_archive_is_accepted_too() {
        let home = temp_home("wrapped");
        let downloader = BackendDownloader::with_base_url(home.clone(), "http://x".into());
        let archive = build_archive_of(&home, &home.join("outer").join(BACKEND_DIR), "wrapped");
        let _ = std::fs::remove_dir_all(home.join("outer"));

        downloader.install(&archive).unwrap();

        assert_eq!(
            std::fs::read_to_string(downloader.backend_path()).unwrap(),
            "wrapped"
        );
        assert!(downloader.backend_ready("0.1.0"));
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn an_archive_without_the_executable_is_refused_and_the_old_tree_kept() {
        let home = temp_home("bad-archive");
        // A live (old) tree that must survive a broken download.
        let downloader = BackendDownloader::with_base_url(home.clone(), "http://x".into());
        std::fs::create_dir_all(downloader.backend_dir()).unwrap();
        std::fs::write(downloader.backend_path(), b"#!/bin/sh\n").unwrap();
        std::fs::write(downloader.backend_dir().join(VERSION_FILE), "0.0.9\n").unwrap();

        let empty = home.join("payload");
        std::fs::create_dir_all(&empty).unwrap();
        std::fs::write(empty.join("README.md"), "no executable here").unwrap();
        let archive = home.join("bad.tar.gz");
        assert!(Command::new("tar")
            .arg("czf")
            .arg(&archive)
            .arg("-C")
            .arg(&empty)
            .arg(".")
            .status()
            .unwrap()
            .success());

        let error = downloader.install(&archive).expect_err("no exe → refuse");
        assert!(error.contains(backend_exe_name()), "{error}");
        assert_eq!(downloader.stored_version().as_deref(), Some("0.0.9"));
        assert!(!home.join(STAGING_DIR).exists(), "staging is cleaned up");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn a_fresh_install_replaces_an_older_tree() {
        let home = temp_home("upgrade");
        let downloader = BackendDownloader::with_base_url(home.clone(), "http://x".into());
        std::fs::create_dir_all(downloader.backend_dir()).unwrap();
        std::fs::write(downloader.backend_path(), b"old").unwrap();
        std::fs::write(downloader.backend_dir().join(VERSION_FILE), "0.0.1\n").unwrap();
        std::fs::write(downloader.backend_dir().join("stale.txt"), b"gone").unwrap();

        let archive = build_archive(&home, "new");
        downloader.install(&archive).unwrap();

        assert_eq!(
            std::fs::read_to_string(downloader.backend_path()).unwrap(),
            "new"
        );
        assert!(!downloader.backend_dir().join("stale.txt").exists());
        assert_eq!(downloader.stored_version().as_deref(), Some("0.1.0"));
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_state_reports_progress_and_terminal_states() {
        let state = BackendDownloadState::default();
        assert_eq!(state.snapshot().state, "idle");

        assert!(state.begin());
        assert!(!state.begin(), "a second run is refused while one is in flight");
        assert_eq!(state.snapshot().state, "running");

        state.progress(DownloadProgress::new(512, 1024));
        let snapshot = state.snapshot();
        assert_eq!((snapshot.downloaded, snapshot.percent), (512, 50));

        state.finish("ready", None);
        let snapshot = state.snapshot();
        assert_eq!(snapshot.state, "ready");
        assert_eq!(snapshot.percent, 50, "the ready snapshot keeps the progress");
        assert!(state.begin(), "the slot is released again");

        state.finish("failed", Some("boom".into()));
        let snapshot = state.snapshot();
        assert_eq!(snapshot.state, "failed");
        assert_eq!(snapshot.error.as_deref(), Some("boom"));
        assert_eq!(snapshot.percent, 0, "a failure carries no progress");
    }

    #[test]
    fn progress_percent_tolerates_an_unknown_total() {
        let progress = DownloadProgress::new(4096, 0);
        assert_eq!(progress.percent, 0);
        let progress = DownloadProgress::new(4096, 2048);
        assert_eq!(progress.percent, 100, "over-reported totals clamp at 100");
    }
}
