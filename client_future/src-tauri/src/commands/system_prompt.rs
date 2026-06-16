//! System Prompt IPC commands — read, write, update.
//!
//! Maps to Python backend:
//! - `GET    /system_prompt` → [`system_prompt_read`]
//! - `PUT    /system_prompt` → [`system_prompt_write`]
//! - `PATCH  /system_prompt` → [`system_prompt_update`]

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use ts_rs::TS;

// ── Request / Response types ────────────────────────────────

/// Payload for writing or updating system prompt files.
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `file_to_content` | `Record<string, string>` | Map of filename to file content |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct PromptFilePayload {
    /// Map of filename (e.g., `"AGENTS.md"`) to file content.
    pub file_to_content: HashMap<String, String>,
}

/// Response after reading all system prompt files.
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `file_to_content` | `Record<string, string>` | Map of filename to file content |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct PromptFileResponse {
    /// Map of filename (e.g., `"AGENTS.md"`) to file content.
    pub file_to_content: HashMap<String, String>,
}

// ── Commands ────────────────────────────────────────────────

/// Read all system prompt files.
///
/// Returns the current content of every system prompt file
/// (e.g., `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`).
///
/// # Arguments
///
/// None.
///
/// # Returns
///
/// `Result<PromptFileResponse, FrontendError>` — A map of filenames to content.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to read prompt files from disk | Yes |
/// | `CONFIG_ERROR` | Prompt file format is invalid | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// const response = await invoke<PromptFileResponse>('system_prompt_read');
/// console.log(response.file_to_content['AGENTS.md']);
/// ```
#[tauri::command]
pub async fn system_prompt_read() -> Result<PromptFileResponse, FrontendError> {
    tracing::info!("system_prompt_read called");
    // TODO: wire up to workspace/prompt_builder
    todo!("system_prompt_read not yet implemented")
}

/// Overwrite system prompt files (full replacement).
///
/// Replaces the entire content of each specified prompt file.
/// Files not included in the payload are left unchanged.
///
/// # Arguments
///
/// * `payload` — A [`PromptFilePayload`] with the files to write.
///
/// # Returns
///
/// `Result<(), FrontendError>` — Unit on success.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to write prompt files to disk | Yes |
/// | `CONFIG_ERROR` | Invalid file content or filename | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// await invoke('system_prompt_write', {
///   payload: {
///     file_to_content: {
///       'AGENTS.md': '# Updated Agents Config',
///       'SOUL.md': '# Updated Soul',
///     },
///   },
/// });
/// ```
#[tauri::command]
pub async fn system_prompt_write(
    payload: PromptFilePayload,
) -> Result<(), FrontendError> {
    tracing::info!(
        file_count = payload.file_to_content.len(),
        "system_prompt_write called"
    );
    // TODO: wire up to workspace/prompt_builder
    todo!("system_prompt_write not yet implemented")
}

/// Partially update system prompt files (merge).
///
/// Updates only the specified fields within the prompt files.
/// Existing content is merged with the new content.
///
/// # Arguments
///
/// * `payload` — A [`PromptFilePayload`] with the partial updates.
///
/// # Returns
///
/// `Result<(), FrontendError>` — Unit on success.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to update prompt files on disk | Yes |
/// | `CONFIG_ERROR` | Invalid merge operation | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// await invoke('system_prompt_update', {
///   payload: {
///     file_to_content: {
///       'AGENTS.md': '# Appended content...',
///     },
///   },
/// });
/// ```
#[tauri::command]
pub async fn system_prompt_update(
    payload: PromptFilePayload,
) -> Result<(), FrontendError> {
    tracing::info!(
        file_count = payload.file_to_content.len(),
        "system_prompt_update called"
    );
    // TODO: wire up to workspace/prompt_builder
    todo!("system_prompt_update not yet implemented")
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prompt_file_payload_deserializes() {
        let json = r##"{"file_to_content":{"AGENTS.md":"# Agents","SOUL.md":"# Soul"}}"##;
        let payload: PromptFilePayload = serde_json::from_str(json).unwrap();
        assert_eq!(payload.file_to_content.len(), 2);
        assert_eq!(
            payload.file_to_content.get("AGENTS.md").map(|s| s.as_str()),
            Some("# Agents")
        );
    }

    #[test]
    fn prompt_file_payload_rejects_missing_map() {
        let json = r#"{}"#;
        let result: Result<PromptFilePayload, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    #[test]
    fn prompt_file_response_serializes() {
        let mut map = HashMap::new();
        map.insert("IDENTITY.md".to_string(), "# Identity".to_string());
        let resp = PromptFileResponse {
            file_to_content: map,
        };
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("IDENTITY.md"));
        assert!(json.contains("# Identity"));
    }

    #[test]
    fn empty_payload_is_valid() {
        let json = r#"{"file_to_content":{}}"#;
        let payload: PromptFilePayload = serde_json::from_str(json).unwrap();
        assert!(payload.file_to_content.is_empty());
    }

    #[test]
    fn prompt_file_payload_round_trip() {
        let mut map = HashMap::new();
        map.insert("AGENTS.md".into(), "# Agents".into());
        map.insert("USER.md".into(), "# User Info".into());
        let original = PromptFilePayload {
            file_to_content: map,
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: PromptFilePayload = serde_json::from_str(&json).unwrap();
        assert_eq!(
            deserialized.file_to_content.get("AGENTS.md"),
            original.file_to_content.get("AGENTS.md")
        );
        assert_eq!(
            deserialized.file_to_content.get("USER.md"),
            original.file_to_content.get("USER.md")
        );
    }
}
