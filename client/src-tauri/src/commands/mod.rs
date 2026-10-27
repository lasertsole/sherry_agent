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
//! | [`system`] | `system_info`, `system_health` | App metadata and health |
//! | [`backend`] | `backend_download_status`, `retry_backend_download` | First-run backend download |
//! | [`cuda`] | `cuda_support`, `cuda_pack_status`, `download_cuda_pack`, `remove_cuda_pack`, `restart_backend` | Optional CUDA pack (model settings tab) |
//! | [`events`] | — | Streaming event types and constants |

pub mod agent;
pub mod backend;
pub mod cuda;
pub mod events;
pub mod session;
pub mod system;
pub mod system_prompt;
