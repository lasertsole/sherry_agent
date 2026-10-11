mod commands;
mod services;
mod utils;

use commands::{agent, session, system, system_prompt};
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
            system_prompt::system_prompt_write,
            system_prompt::system_prompt_update,
            // System
            system::system_info,
            system::system_health,
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

            // Packaged builds ship the backend next to the app (see the bundle's
            // `resources`); a checkout run has no such file and uses `python -m
            // server` instead. EMA_AUTO_START_BACKEND can force the checkout
            // path in a dev build.
            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            let packaged = PythonProcessManager::packaged_backend(&resource_dir);
            tracing::info!(
                resources = %resource_dir.display(),
                found = packaged.is_some(),
                "backend resource probe"
            );
            let project_root = std::env::var("EMA_PROJECT_ROOT")
                .unwrap_or_else(|_| {
                    // Default: two levels up from src-tauri/
                    std::env::current_dir()
                        .ok()
                        .and_then(|p| p.parent().map(|pp| pp.to_path_buf()))
                        .map(|p| p.to_string_lossy().to_string())
                        .unwrap_or_else(|| ".".to_string())
                });
            let auto_start_requested = std::env::var("EMA_AUTO_START_BACKEND")
                .map(|v| v == "true" || v == "1")
                .unwrap_or(false);

            let command = match packaged {
                Some(exe) => {
                    tracing::info!(exe = %exe.display(), "Using the bundled Python backend");
                    BackendCommand::Packaged(exe)
                }
                None => BackendCommand::Checkout {
                    python: std::env::var("EMA_PYTHON").unwrap_or_else(|_| "python".to_string()),
                    root: std::path::PathBuf::from(&project_root),
                },
            };
            let process_mgr = PythonProcessManager::new(command, app_home.clone());

            // A packaged backend ALWAYS starts (there is no other way for the
            // app to work); the checkout path needs the env opt-in or an
            // explicit request, so a dev run stays exactly as it was.
            let bundled = process_mgr.is_packaged();
            if bundled || auto_start_requested {
                match process_mgr.start() {
                    Ok(true) => tracing::info!(
                        home = %app_home.display(),
                        bundled,
                        "Python backend auto-started"
                    ),
                    Ok(false) => tracing::info!("Python backend already running"),
                    Err(e) => tracing::warn!("Failed to auto-start Python backend: {e}"),
                }
            } else {
                tracing::info!(
                    "No bundled backend and EMA_AUTO_START_BACKEND is unset — expecting an externally started backend on 127.0.0.1:8080"
                );
            }

            // Store the process manager as app state for cleanup
            app.manage(process_mgr);

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
