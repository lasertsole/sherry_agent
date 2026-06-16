//! Tauri IPC command modules.
//!
//! Each submodule contains `#[tauri::command]` functions that are
//! registered in [`crate::run()`] via `.invoke_handler()`.
//!
//! # Available Commands
//!
//! | Module | Commands | Description |
//! |--------|----------|-------------|
//! | [`agent`] | `agent_chat` | Send messages and get agent responses |
//! | [`session`] | `session_clear`, `session_history` | Session lifecycle management |
//! | [`system_prompt`] | `system_prompt_read/write/update` | System prompt CRUD |
//! | [`character`] | `character_read/write/update` | Character config CRUD |
//! | [`system`] | `system_info`, `system_health` | App metadata and health |
//! | [`events`] | — | Streaming event types and constants |

pub mod agent;
pub mod character;
pub mod events;
pub mod session;
pub mod system;
pub mod system_prompt;
