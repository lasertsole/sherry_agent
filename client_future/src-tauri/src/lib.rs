mod commands;
mod utils;

use commands::{agent, character, session, system, system_prompt};
use utils::logger::setup_logger;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Initialize tracing before Tauri starts (early init pattern).
    // This makes structured logging available during app bootstrap.
    let tracing_builder = setup_logger();

    tauri::Builder::default()
        .plugin(tracing_builder.build())
        .invoke_handler(tauri::generate_handler![
            // Agent
            agent::agent_chat,
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
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
