//! CUDA pack IPC commands (packing-plan B1/B3/E2) — detection, status, the
//! opt-in download, uninstall and the backend restart that activates it.
//!
//! The pack is Linux-only and optional: `cuda_support` gates the whole card
//! (the model settings tab renders it only when the host has an NVIDIA
//! driver), and the download runs on a background thread exactly like the
//! first-run backend download, reporting through the
//! `cuda-pack-download-*` Tauri events (the pull half lives here).

use crate::services::backend_downloader::DownloadEvents;
use crate::services::cuda_detect::{self, CudaSupport};
use crate::services::cuda_pack_downloader::{
    CudaPackDownloadState, CudaPackDownloader, CudaPackStatus, TauriCudaPackEvents,
};
use tauri::Manager;

/// The host's CUDA support (the model settings tab's visibility gate).
#[tauri::command]
pub fn cuda_support() -> CudaSupport {
    cuda_detect::detect()
}

/// The CUDA pack download's current state.
///
/// `state` is `idle` (nothing downloaded — no pack yet, or this is a
/// development run) / `running` / `ready` / `failed`.
#[tauri::command]
pub fn cuda_pack_status(state: tauri::State<'_, CudaPackDownloadState>) -> CudaPackStatus {
    state.snapshot()
}

/// Start the CUDA pack download in the background.
///
/// A no-op while a download is already in flight. The pack is Linux-only: on
/// any other platform (or a development build) this answers an error the UI
/// never offers a button for.
#[tauri::command]
pub fn download_cuda_pack(app: tauri::AppHandle) -> Result<(), String> {
    if cfg!(dev) {
        return Err("this is a development build: no CUDA pack is managed here".into());
    }
    if !CudaPackDownloader::platform_supported() {
        return Err("the CUDA pack is only published for Linux".into());
    }
    if !app
        .state::<CudaPackDownloadState>()
        .begin()
    {
        tracing::info!("a CUDA pack download is already in flight; not starting another");
        return Ok(());
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        let app_home = handle
            .path()
            .app_data_dir()
            .unwrap_or_else(|_| std::path::PathBuf::from("."));
        let downloader = CudaPackDownloader::new(app_home);
        let events = TauriCudaPackEvents::new(handle.clone());
        match downloader.ensure(&events) {
            Ok(dir) => {
                tracing::info!(dir = %dir.display(), "CUDA pack installed");
                handle.state::<CudaPackDownloadState>().finish("ready", None);
                events.complete();
            }
            Err(error) => {
                tracing::warn!("CUDA pack download failed: {error}");
                handle
                    .state::<CudaPackDownloadState>()
                    .finish("failed", Some(error.clone()));
                events.failed(&error);
            }
        }
    });
    Ok(())
}

/// Remove the installed CUDA pack; the backend falls back to CPU on its next
/// start. The backend keeps running with CUDA until then (torch is loaded).
#[tauri::command]
pub fn remove_cuda_pack(app: tauri::AppHandle) -> Result<(), String> {
    let app_home = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app-data directory: {e}"))?;
    let downloader = CudaPackDownloader::new(app_home);
    downloader.remove()
}

/// Stop and restart the managed Python backend (the "install and restart"
/// action after a pack download: torch resolves the CUDA libraries at import,
/// so a running backend only picks the pack up on a fresh process).
#[tauri::command]
pub fn restart_backend(state: tauri::State<'_, crate::services::python_process::PythonProcessManager>) -> Result<(), String> {
    state.stop();
    state.start().map(|spawned| {
        tracing::info!(spawned, "Python backend restarted");
    })
}
