//! Agent IPC commands — chat, streaming, and agent lifecycle.
//!
//! Maps to Python backend:
//! - `POST /sessions/agent/sse` → [`agent_chat`]
//!
//! # Streaming Events
//!
//! The agent uses Tauri events for streaming responses. See [`super::events`]
//! for the complete event lifecycle.

use crate::utils::error::FrontendError;
use serde::{Deserialize, Serialize};
use ts_rs::TS;

// ── Request / Response types ────────────────────────────────

/// Multi-modal message payload sent from the frontend to the agent.
///
/// # Fields
///
/// | Field | Type | Required | Description |
/// |-------|------|----------|-------------|
/// | `session_id` | `string` | Yes | Unique session identifier |
/// | `text` | `string \| null` | No | Text message content |
/// | `image_base64_list` | `string[]` | No | Base64-encoded image data |
///
/// At least one of `text` or `image_base64_list` should be provided.
///
/// # Example
///
/// ```json
/// {
///   "session_id": "main",
///   "text": "Hello, how are you?",
///   "image_base64_list": []
/// }
/// ```
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct ChatRequest {
    /// Unique session identifier.
    pub session_id: String,
    /// Optional text message content.
    pub text: Option<String>,
    /// Optional list of base64-encoded images for multi-modal input.
    #[serde(default)]
    pub image_base64_list: Vec<String>,
}

/// A single streaming chunk returned by the agent.
///
/// The frontend receives a `Vec<ChatChunk>` (or individual chunks
/// via Tauri events for true streaming).
///
/// # Fields
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `content` | `string` | Text fragment for this chunk |
/// | `done` | `boolean` | `true` if this is the final chunk |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct ChatChunk {
    /// The text fragment for this chunk.
    pub content: String,
    /// Whether this is the final chunk.
    #[serde(default)]
    pub done: bool,
}

// ── Commands ────────────────────────────────────────────────

/// Send a chat message to the agent and receive a streamed response.
///
/// This command initiates an agent conversation turn. The agent processes
/// the message through its LangGraph pipeline (context building → LLM call
/// → tool execution → response generation) and returns the result as a
/// sequence of [`ChatChunk`]s.
///
/// # Arguments
///
/// * `request` — A [`ChatRequest`] containing the session ID and user message.
///
/// # Returns
///
/// `Result<Vec<ChatChunk>, FrontendError>` — A vector of response chunks.
/// For real-time streaming, listen to the Tauri events defined in
/// [`super::events`] instead.
///
/// # Errors
///
/// | Error Code | Description | Retryable |
/// |------------|-------------|-----------|
/// | `AGENT_ERROR` | Agent pipeline failure (tool loop, LangGraph error) | No |
/// | `MODEL_ERROR` | LLM API call failed (timeout, connection refused) | Yes |
/// | `SESSION_ERROR` | Invalid or expired session ID | No |
/// | `RAG_ERROR` | Knowledge retrieval failure | No |
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// // Simple text chat
/// const chunks = await invoke<ChatChunk[]>('agent_chat', {
///   request: {
///     session_id: 'main',
///     text: 'Hello!',
///     image_base64_list: [],
///   },
/// });
///
/// // Multi-modal chat with images
/// const chunks = await invoke<ChatChunk[]>('agent_chat', {
///   request: {
///     session_id: 'main',
///     text: 'Describe this image',
///     image_base64_list: [base64ImageData],
///   },
/// });
/// ```
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

    #[test]
    fn chat_request_round_trip() {
        let original = ChatRequest {
            session_id: "main".into(),
            text: Some("How's the weather?".into()),
            image_base64_list: vec![],
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: ChatRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.session_id, original.session_id);
        assert_eq!(deserialized.text, original.text);
    }

    #[test]
    fn chat_chunk_round_trip() {
        let original = ChatChunk {
            content: "The weather is sunny.".into(),
            done: false,
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: ChatChunk = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.content, original.content);
        assert_eq!(deserialized.done, original.done);
    }
}
