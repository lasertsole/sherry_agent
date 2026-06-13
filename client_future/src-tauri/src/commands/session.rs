//! Session IPC commands — clear, history retrieval.
//!
//! Maps to Python backend:
//! - `DELETE /sessions` → `session_clear`
//! - `GET /n_turns_history_messages` → `session_history`

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};

// ── Request / Response types ────────────────────────────────

/// Request to clear a session's state.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClearSessionRequest {
    pub session_id: String,
}

/// Request to retrieve conversation history.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HistoryRequest {
    pub session_id: String,
    /// Number of recent turns to retrieve. `None` = all.
    pub last_turn_count: Option<usize>,
}

/// A single history message.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HistoryMessage {
    pub role: String,
    pub content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
}

// ── Commands ────────────────────────────────────────────────

/// Clear all state for a given session.
#[tauri::command]
pub async fn session_clear(
    request: ClearSessionRequest,
) -> Result<(), FrontendError> {
    tracing::info!(session_id = %request.session_id, "session_clear called");
    // TODO: wire up to session store
    todo!("session_clear not yet implemented")
}

/// Retrieve conversation history for a session.
#[tauri::command]
pub async fn session_history(
    request: HistoryRequest,
) -> Result<Vec<HistoryMessage>, FrontendError> {
    tracing::info!(
        session_id = %request.session_id,
        last_turn_count = ?request.last_turn_count,
        "session_history called"
    );
    // TODO: wire up to session store
    todo!("session_history not yet implemented")
}

// ── Tests (written FIRST to define the contract) ────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clear_session_request_deserializes() {
        let json = r#"{"session_id":"main"}"#;
        let req: ClearSessionRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.session_id, "main");
    }

    #[test]
    fn clear_session_request_rejects_missing_id() {
        let json = r#"{}"#;
        let result: Result<ClearSessionRequest, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    #[test]
    fn history_request_deserializes_with_count() {
        let json = r#"{"session_id":"s1","last_turn_count":5}"#;
        let req: HistoryRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.session_id, "s1");
        assert_eq!(req.last_turn_count, Some(5));
    }

    #[test]
    fn history_request_deserializes_without_count() {
        let json = r#"{"session_id":"s1"}"#;
        let req: HistoryRequest = serde_json::from_str(json).unwrap();
        assert!(req.last_turn_count.is_none());
    }

    #[test]
    fn history_message_serializes_with_timestamp() {
        let msg = HistoryMessage {
            role: "user".into(),
            content: "hello".into(),
            timestamp: Some("2024-01-01T00:00:00Z".into()),
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains("\"role\":\"user\""));
        assert!(json.contains("timestamp"));
    }

    #[test]
    fn history_message_omits_none_timestamp() {
        let msg = HistoryMessage {
            role: "assistant".into(),
            content: "hi".into(),
            timestamp: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(!json.contains("timestamp"));
    }
}
