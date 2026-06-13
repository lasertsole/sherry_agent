//! Character IPC commands — read, write, update character configuration.
//!
//! Maps to Python backend:
//! - `GET    /character` → `character_read`
//! - `PUT    /character` → `character_write`
//! - `PATCH  /character` → `character_update`

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

// ── Request / Response types ────────────────────────────────

/// Character data: each key is a section name, value is a map of
/// field → content pairs.
pub type CharacterData = HashMap<String, HashMap<String, String>>;

/// Payload for writing or updating character configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CharacterPayload {
    pub character_data: CharacterData,
}

/// Response after reading character configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CharacterResponse {
    pub character_data: CharacterData,
}

// ── Commands ────────────────────────────────────────────────

/// Read the current character configuration.
#[tauri::command]
pub async fn character_read() -> Result<CharacterResponse, FrontendError> {
    tracing::info!("character_read called");
    // TODO: wire up to workspace/character
    todo!("character_read not yet implemented")
}

/// Overwrite character configuration (full replacement).
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

// ── Tests (written FIRST to define the contract) ────────────

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
}
