//! Session IPC commands — clear, history retrieval.
//!
//! Maps to Python backend:
//! - `DELETE /sessions` → [`session_clear`]
//! - `GET /n_turns_history_messages` → [`session_history`]

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};
use ts_rs::TS;

// ── Request / Response types ────────────────────────────────

/// Request to clear a session's state.
///
/// | Field | Type | Required | Description |
/// |-------|------|----------|-------------|
/// | `session_id` | `string` | Yes | Session to clear |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct ClearSessionRequest {
    /// The session ID to clear.
    pub session_id: String,
}

/// Request to retrieve conversation history.
///
/// | Field | Type | Required | Description |
/// |-------|------|----------|-------------|
/// | `session_id` | `string` | Yes | Session to query |
/// | `last_turn_count` | `number \| null` | No | Number of recent turns (null = all) |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct HistoryRequest {
    /// The session ID to query history for.
    pub session_id: String,
    /// Number of recent turns to retrieve. `None` = all.
    pub last_turn_count: Option<usize>,
}

/// A single history message in a conversation.
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `role` | `string` | `"user"` or `"assistant"` |
/// | `content` | `string` | Message text content |
/// | `timestamp` | `string \| null` | ISO 8601 timestamp (omitted if absent) |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct HistoryMessage {
    /// Message role: `"user"` or `"assistant"`.
    pub role: String,
    /// Message text content.
    pub content: String,
    /// Optional ISO 8601 timestamp.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
}

// ── Commands ────────────────────────────────────────────────

/// Clear all state for a given session.
///
/// Removes conversation history, checkpointer data, and any cached
/// context associated with the session.
///
/// # Arguments
///
/// * `request` — A [`ClearSessionRequest`] with the session ID.
///
/// # Returns
///
/// `Result<(), FrontendError>` — Unit on success.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `SESSION_ERROR` | Session not found or already cleared | No |
/// | `DATABASE_ERROR` | Failed to delete session data from SQLite | Yes |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// await invoke('session_clear', {
///   request: { session_id: 'main' },
/// });
/// ```
#[tauri::command]
pub async fn session_clear(
    request: ClearSessionRequest,
) -> Result<(), FrontendError> {
    tracing::info!(session_id = %request.session_id, "session_clear called");
    // TODO: wire up to session store
    todo!("session_clear not yet implemented")
}

/// Retrieve conversation history for a session.
///
/// Returns the last N turns (or all turns if `last_turn_count` is null)
/// as an ordered list of messages.
///
/// # Arguments
///
/// * `request` — A [`HistoryRequest`] with the session ID and optional turn count.
///
/// # Returns
///
/// `Result<Vec<HistoryMessage>, FrontendError>` — Ordered list of messages,
/// oldest first.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `SESSION_ERROR` | Session not found | No |
/// | `DATABASE_ERROR` | Failed to query history from SQLite | Yes |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// // Get last 10 turns
/// const messages = await invoke<HistoryMessage[]>('session_history', {
///   request: { session_id: 'main', last_turn_count: 10 },
/// });
///
/// // Get all history
/// const allMessages = await invoke<HistoryMessage[]>('session_history', {
///   request: { session_id: 'main', last_turn_count: null },
/// });
/// ```
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

// ── Tests ───────────────────────────────────────────────────

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

    #[test]
    fn clear_session_request_round_trip() {
        let original = ClearSessionRequest {
            session_id: "session_abc".into(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: ClearSessionRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.session_id, original.session_id);
    }

    #[test]
    fn history_message_round_trip() {
        let original = HistoryMessage {
            role: "assistant".into(),
            content: "I'd be happy to help!".into(),
            timestamp: Some("2024-06-15T10:30:00Z".into()),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: HistoryMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.role, original.role);
        assert_eq!(deserialized.content, original.content);
        assert_eq!(deserialized.timestamp, original.timestamp);
    }
}
