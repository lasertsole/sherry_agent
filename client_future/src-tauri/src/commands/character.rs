//! Character IPC commands — read, write, update character configuration.
//!
//! Maps to Python backend:
//! - `GET    /character` → [`character_read`]
//! - `PUT    /character` → [`character_write`]
//! - `PATCH  /character` → [`character_update`]

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use ts_rs::TS;

// ── Request / Response types ────────────────────────────────

/// Character data: each key is a section name, value is a map of
/// field → content pairs.
///
/// ```typescript
/// type CharacterData = Record<string, Record<string, string>>;
/// // Example: { "identity": { "name": "Sherry", "personality": "cheerful" } }
/// ```
pub type CharacterData = HashMap<String, HashMap<String, String>>;

/// Payload for writing or updating character configuration.
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `character_data` | `CharacterData` | Nested map of section → field → value |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct CharacterPayload {
    /// The character configuration data.
    pub character_data: CharacterData,
}

/// Response after reading character configuration.
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `character_data` | `CharacterData` | Nested map of section → field → value |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct CharacterResponse {
    /// The character configuration data.
    pub character_data: CharacterData,
}

// ── Commands ────────────────────────────────────────────────

/// Read the current character configuration.
///
/// Returns all character sections and their field values
/// (e.g., identity, personality, appearance).
///
/// # Arguments
///
/// None.
///
/// # Returns
///
/// `Result<CharacterResponse, FrontendError>` — The full character config.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to read character file from disk | Yes |
/// | `CONFIG_ERROR` | Character file is malformed | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// const response = await invoke<CharacterResponse>('character_read');
/// console.log(response.character_data.identity?.name);
/// ```
#[tauri::command]
pub async fn character_read() -> Result<CharacterResponse, FrontendError> {
    tracing::info!("character_read called");
    // TODO: wire up to workspace/character
    todo!("character_read not yet implemented")
}

/// Overwrite character configuration (full replacement).
///
/// Replaces the entire character configuration with the provided data.
///
/// # Arguments
///
/// * `payload` — A [`CharacterPayload`] with the new character data.
///
/// # Returns
///
/// `Result<(), FrontendError>` — Unit on success.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to write character file to disk | Yes |
/// | `CONFIG_ERROR` | Invalid character data structure | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// await invoke('character_write', {
///   payload: {
///     character_data: {
///       identity: { name: 'Sherry', personality: 'cheerful' },
///       appearance: { hair: 'blonde', eyes: 'blue' },
///     },
///   },
/// });
/// ```
#[tauri::command]
pub async fn character_write(
    payload: CharacterPayload,
) -> Result<(), FrontendError> {
    tracing::info!(
        sections = payload.character_data.len(),
        "character_write called"
    );
    // TODO: wire up to workspace/character
    todo!("character_write not yet implemented")
}

/// Partially update character configuration (merge).
///
/// Merges the provided fields into the existing character config.
/// Only the specified sections/fields are updated.
///
/// # Arguments
///
/// * `payload` — A [`CharacterPayload`] with partial updates.
///
/// # Returns
///
/// `Result<(), FrontendError>` — Unit on success.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `IO_ERROR` | Failed to update character file on disk | Yes |
/// | `CONFIG_ERROR` | Invalid merge operation | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// await invoke('character_update', {
///   payload: {
///     character_data: {
///       identity: { personality: 'energetic' },
///     },
///   },
/// });
/// ```
#[tauri::command]
pub async fn character_update(
    payload: CharacterPayload,
) -> Result<(), FrontendError> {
    tracing::info!(
        sections = payload.character_data.len(),
        "character_update called"
    );
    // TODO: wire up to workspace/character
    todo!("character_update not yet implemented")
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_character_data() -> CharacterData {
        let mut inner = HashMap::new();
        inner.insert("name".to_string(), "Sherry".to_string());
        inner.insert("personality".to_string(), "cheerful".to_string());

        let mut data = HashMap::new();
        data.insert("identity".to_string(), inner);
        data
    }

    #[test]
    fn character_payload_deserializes() {
        let json = r#"{
            "character_data": {
                "identity": {
                    "name": "Sherry",
                    "personality": "cheerful"
                }
            }
        }"#;
        let payload: CharacterPayload = serde_json::from_str(json).unwrap();
        let identity = payload.character_data.get("identity").unwrap();
        assert_eq!(identity.get("name").unwrap(), "Sherry");
    }

    #[test]
    fn character_payload_rejects_missing_data() {
        let json = r#"{}"#;
        let result: Result<CharacterPayload, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    #[test]
    fn character_response_serializes() {
        let resp = CharacterResponse {
            character_data: sample_character_data(),
        };
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"identity\""));
        assert!(json.contains("Sherry"));
    }

    #[test]
    fn empty_character_data_is_valid() {
        let json = r#"{"character_data":{}}"#;
        let payload: CharacterPayload = serde_json::from_str(json).unwrap();
        assert!(payload.character_data.is_empty());
    }

    #[test]
    fn character_payload_round_trip() {
        let original = CharacterPayload {
            character_data: sample_character_data(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: CharacterPayload = serde_json::from_str(&json).unwrap();
        let identity = deserialized.character_data.get("identity").unwrap();
        assert_eq!(identity.get("name").unwrap(), "Sherry");
        assert_eq!(identity.get("personality").unwrap(), "cheerful");
    }

    #[test]
    fn character_response_round_trip() {
        let original = CharacterResponse {
            character_data: sample_character_data(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: CharacterResponse = serde_json::from_str(&json).unwrap();
        assert_eq!(
            deserialized.character_data.len(),
            original.character_data.len()
        );
    }
}
