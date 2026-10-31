mod commands;
mod services;
mod utils;

use commands::{agent, backend, cuda, session, system, system_prompt};
use services::backend_downloader::{ensure_and_launch, BackendDownloader, BackendDownloadState};
use services::cuda_pack_downloader::CudaPackDownloadState;
use services::python_bridge::PythonBridge;
use services::python_process::{BackendCommand, PythonProcessManager};
use utils::config::AppConfig;
use utils::logger::setup_logger;

use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};
use tauri_plugin_global_shortcut::GlobalShortcutExt;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Initialize tracing before Tauri starts (early init pattern).
    // This makes structured logging available during app bootstrap.
    let tracing_builder = setup_logger();

    // Load configuration and build the Python backend bridge.
    let config = AppConfig::from_env();
    let bridge = PythonBridge::new(
        config.python_backend_url.clone(),
        config.python_backend_timeout_secs,
    );

    tauri::Builder::default()
        .plugin(tracing_builder.build())
        // Desktop enhancement plugins
        // (tauri-plugin-shell removed — nothing used the frontend
        // shell API; the Python backend is spawned via std::process::Command
        // in services::python_process, so the generic allow-spawn/allow-execute
        // capability was pure attack surface)
        .plugin(tauri_plugin_notification::init())
        // Native folder picker for the project-directory chip (registered here,
        // granted in capabilities/default.json as dialog:allow-open only).
        .plugin(tauri_plugin_dialog::init())
        // The file tree's desktop-only actions: reveal a project file in the
        // OS file manager and open it with the system's default application.
        // Granted narrowly in capabilities/default.json (reveal + open-path
        // only, no open-url / no generic shell).
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Focus the existing window when a second instance is launched.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .manage(config)
        .manage(bridge)
        .invoke_handler(tauri::generate_handler![
            // Agent
            agent::agent_chat,
            agent::agent_stop,
            // Session
            session::session_clear,
            session::session_history,
            // System Prompt
            system_prompt::system_prompt_read,
            system_prompt::system_prompt_read_template,
            system_prompt::system_prompt_write,
            system_prompt::system_prompt_update,
            // System
            system::system_info,
            system::system_health,
            // Backend download (first run of a packaged app)
            backend::backend_download_status,
            backend::retry_backend_download,
            // Optional CUDA pack (model settings tab; Linux only)
            cuda::cuda_support,
            cuda::cuda_pack_status,
            cuda::download_cuda_pack,
            cuda::remove_cuda_pack,
            cuda::restart_backend,
        ])
        .setup(|app| {
            // ── System tray ──────────────────────────────────────
            let show_item = MenuItemBuilder::with_id("show", "Show").build(app)?;
            let hide_item = MenuItemBuilder::with_id("hide", "Hide").build(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "Quit").build(app)?;

            let tray_menu = MenuBuilder::new(app)
                .item(&show_item)
                .item(&hide_item)
                .separator()
                .item(&quit_item)
                .build()?;

            let _tray = TrayIconBuilder::new()
                .menu(&tray_menu)
                .tooltip("EMA AI Agent")
                .on_menu_event(|app: &tauri::AppHandle, event: tauri::menu::MenuEvent| {
                    match event.id().as_ref() {
                        "show" => {
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                        "hide" => {
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.hide();
                            }
                        }
                        "quit" => {
                            tracing::info!("Quit requested via tray menu");
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .build(app)?;

            // ── Global shortcut (Alt+Space = toggle window) ──────
            let handle = app.handle().clone();
            app.handle()
                .plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |_app, _shortcut, event| {
                            if event.state == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                                if let Some(window) = handle.get_webview_window("main") {
                                    if window.is_visible().unwrap_or(false) {
                                        let _ = window.hide();
                                    } else {
                                        let _ = window.show();
                                        let _ = window.set_focus();
                                    }
                                }
                            }
                        })
                        .build(),
                )?;

            // Register Alt+Space after plugin is initialized
            if let Ok(shortcut) = "Alt+Space".parse::<tauri_plugin_global_shortcut::Shortcut>() {
                let _ = app.global_shortcut().register(shortcut);
            }

            // ── Python backend process ─────
            // The writable app-data root the backend keeps everything in
            // (sessions, memory, logs, learned skills, .env). The bundle is
            // replaced by updates, so nothing of the user's may live there.
            let app_home = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            if let Err(e) = std::fs::create_dir_all(&app_home) {
                tracing::warn!(dir = %app_home.display(), "Failed to create the app-data directory: {e}");
            }

            if cfg!(dev) {
                // Development run (`pnpm tauri dev`, or a plain `cargo build`):
                // the frozen backend is neither shipped nor downloaded here — the
                // backend is the checkout's `python -m server`, started only with
                // the EMA_AUTO_START_BACKEND opt-in, so a dev run behaves exactly
                // as it always did.
                let project_root = std::env::var("EMA_PROJECT_ROOT").unwrap_or_else(|_| {
                    // Default: two levels up from src-tauri/
                    std::env::current_dir()
                        .ok()
                        .and_then(|p| p.parent().map(|pp| pp.to_path_buf()))
                        .map(|p| p.to_string_lossy().to_string())
                        .unwrap_or_else(|| ".".to_string())
                });
                let command = BackendCommand::Checkout {
                    python: std::env::var("EMA_PYTHON").unwrap_or_else(|_| "python".to_string()),
                    root: std::path::PathBuf::from(&project_root),
                };
                let process_mgr = PythonProcessManager::new(command, app_home.clone());
                let auto_start_requested = std::env::var("EMA_AUTO_START_BACKEND")
                    .map(|v| v == "true" || v == "1")
                    .unwrap_or(false);
                if auto_start_requested {
                    match process_mgr.start() {
                        Ok(true) => tracing::info!(
                            home = %app_home.display(),
                            "Checkout Python backend auto-started"
                        ),
                        Ok(false) => tracing::info!("Python backend already running"),
                        Err(e) => tracing::warn!("Failed to auto-start Python backend: {e}"),
                    }
                } else {
                    tracing::info!(
                        "Development build without EMA_AUTO_START_BACKEND — expecting an externally started backend on 127.0.0.1:8080"
                    );
                }
                app.manage(process_mgr);
            } else {
                // Packaged desktop app. The unified installer ships the backend
                // INSIDE the bundle (`resource_dir/sherry-backend/`, packing-plan
                // A1/A2): the tree's VERSION gates it exactly like the
                // downloaded one, so a stale bundle never serves. The download
                // flow stays as the fallback — an older installer, a mismatched
                // bundle, or a manually stripped install all land there.
                let version = app.package_info().version.to_string();
                let bundled_tree = app
                    .path()
                    .resource_dir()
                    .ok()
                    .map(|dir| dir.join(services::backend_downloader::BACKEND_DIR));
                let bundled_exe = bundled_tree.as_ref().and_then(|tree| {
                    let exe = tree.join(services::backend_downloader::backend_exe_name());
                    let version_matches = std::fs::read_to_string(tree.join(services::backend_downloader::VERSION_FILE))
                        .map(|stored| services::backend_downloader::versions_match(&stored, &version))
                        .unwrap_or(false);
                    (exe.is_file() && version_matches).then_some(exe)
                });

                // Both branches answer the download-status IPC; in the bundled
                // branch the state simply stays "ready" and no download starts.
                // The CUDA pack's state is likewise always managed: the pack is
                // optional and its commands answer "idle" until a download.
                app.manage(BackendDownloadState::default());
                app.manage(CudaPackDownloadState::default());
                // Both bundled/downloaded launches hand the manager the pack
                // DIRECTORY: the per-start resolver decides (per R1/R6) whether
                // the pack's VERSION matches the backend and joins its
                // libraries to the loader path — so restart_backend picks a
                // freshly downloaded pack up without rebuilding the manager.
                let cuda_pack_dir = Some(app_home.join(services::cuda_pack_downloader::PACK_DIR));

                match bundled_exe {
                    Some(exe) => {
                        tracing::info!(exe = %exe.display(), "Using the bundled Python backend (no download)");
                        app.manage(PythonProcessManager::new(
                            BackendCommand::Packaged {
                                exe,
                                cuda_pack_dir: cuda_pack_dir.clone(),
                            },
                            app_home.clone(),
                        ));
                        if let Err(e) = app.state::<PythonProcessManager>().start() {
                            tracing::warn!("Failed to start the bundled Python backend: {e}");
                        }
                    }
                    None => {
                        // The installer ships only the SPA + this shell, so the
                        // backend lives in the app-data home — downloaded from
                        // GitHub Releases on the first launch (or when the
                        // installed tree is older than this build). There is no
                        // other way for the app to work, so it ALWAYS starts.
                        let downloader = BackendDownloader::new(app_home.clone());
                        app.manage(PythonProcessManager::new(
                            BackendCommand::Packaged {
                                exe: downloader.backend_path(),
                                cuda_pack_dir: cuda_pack_dir.clone(),
                            },
                            app_home.clone(),
                        ));
                        if let Err(e) = ensure_and_launch(app.handle()) {
                            tracing::warn!("Failed to ensure the Python backend: {e}");
                        }
                    }
                }
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
