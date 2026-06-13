//! Agent IPC commands — chat, streaming, and agent lifecycle.
//!
//! Maps to Python backend:
//! - `POST /sessions/agent/sse` → `agent_chat`

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};

// ── Request / Response types ────────────────────────────────

/// Multi-modal message payload from the frontend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    pub session_id: String,
    pub text: Option<String>,
    #[serde(default)]
    pub image_base64_list: Vec<String>,
}

/// A single SSE chunk returned to the frontend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatChunk {
    /// The text fragment for this chunk.
    pub content: String,
    /// Whether this is the final chunk.
    #[serde(default)]
    pub done: bool,
}

// ── Commands ────────────────────────────────────────────────

/// Send a chat message and receive the agent's response.
///
/// In the Python backend this was an SSE stream. In Tauri we can
/// use Tauri events or return a `Vec<ChatChunk>` for simplicity.
#[tauri::command]
pub async fn agent_chat(
    request: ChatRequest,
) -> Result<Vec<ChatChunk>, FrontendError> {
    tracing::info!(session_id = %request.session_id, "agent_chat called");
    // TODO: wire up to the agent core module
    todo!("agent_chat not yet implemented")
}

// ── Tests (written FIRST to define the contract) ────────────

#[cfg(test)]
mod tests {
    use super::*;

    // -- Serialization tests --

    #[test]
    fn chat_request_deserializes_with_required_fields() {
        let json = r#"{"session_id":"s1","text":"hello"}"#;
        let req: ChatRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.session_id, "s1");
        assert_eq!(req.text.as_deref(), Some("hello"));
        assert!(req.image_base64_list.is_empty());
    }

    #[test]
    fn chat_request_deserializes_with_images() {
        let json = r#"{"session_id":"s1","text":"describe","image_base64_list":["abc"]}"#;
        let req: ChatRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.image_base64_list.len(), 1);
    }

    #[test]
    fn chat_request_rejects_missing_session_id() {
        let json = r#"{"text":"hello"}"#;
        let result: Result<ChatRequest, _> = serde_json::from_str(json);
        assert!(result.is_err(), "missing session_id should fail");
    }

    #[test]
    fn chat_chunk_serializes_correctly() {
        let chunk = ChatChunk {
            content: "Hi!".into(),
            done: false,
        };
        let json = serde_json::to_string(&chunk).unwrap();
        assert!(json.contains("\"content\":\"Hi!\""));
        assert!(json.contains("\"done\":false"));
    }

    #[test]
    fn chat_chunk_done_flag_serializes() {
        let chunk = ChatChunk {
            content: String::new(),
            done: true,
        };
        let json = serde_json::to_string(&chunk).unwrap();
        assert!(json.contains("\"done\":true"));
    }
}
