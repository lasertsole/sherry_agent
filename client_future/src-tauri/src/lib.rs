mod commands;
mod services;
mod utils;

use commands::{agent, character, session, system, system_prompt};
use services::python_bridge::PythonBridge;
use services::python_process::PythonProcessManager;
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
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
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
            // Character
            character::character_read,
            character::character_write,
            character::character_update,
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

            // ── Python backend process (optional auto-spawn) ─────
            let project_root = std::env::var("EMA_PROJECT_ROOT")
                .unwrap_or_else(|_| {
                    // Default: two levels up from src-tauri/
                    std::env::current_dir()
                        .ok()
                        .and_then(|p| p.parent().map(|pp| pp.to_path_buf()))
                        .map(|p| p.to_string_lossy().to_string())
                        .unwrap_or_else(|| ".".to_string())
                });

            let process_mgr = PythonProcessManager::new(project_root.clone());

            // Auto-spawn if EMA_AUTO_START_BACKEND=true
            if std::env::var("EMA_AUTO_START_BACKEND")
                .map(|v| v == "true" || v == "1")
                .unwrap_or(false)
            {
                match process_mgr.start() {
                    Ok(true) => tracing::info!("Python backend auto-started"),
                    Ok(false) => tracing::info!("Python backend already running"),
                    Err(e) => tracing::warn!("Failed to auto-start Python backend: {e}"),
                }
            }

            // Store the process manager as app state for cleanup
            app.manage(process_mgr);

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
