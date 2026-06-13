//! Tauri IPC command modules.
//!
//! Each submodule contains `#[tauri::command]` functions that are
//! registered in [`crate::run()`] via `.invoke_handler()`.

pub mod agent;
pub mod character;
pub mod session;
pub mod system;
pub mod system_prompt;
