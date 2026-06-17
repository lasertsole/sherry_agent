//! Agent IPC commands — chat, streaming, and agent lifecycle.
//!
//! Maps to Python backend:
//! - `POST /sessions/agent/sse` → [`agent_chat`]
//! - `POST /sessions/agent/sse/stop` → [`agent_stop`]
//!
//! # Streaming Events
//!
//! The agent uses Tauri events for streaming responses. See [`super::events`]
//! for the complete event lifecycle.

use super::events::*;
use crate::services::python_bridge::PythonBridge;
use crate::utils::error::FrontendError;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tauri::Emitter;
use ts_rs::TS;
use uuid::Uuid;

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

/// Request to stop an ongoing agent generation.
///
/// | Field | Type | Required | Description |
/// |-------|------|----------|-------------|
/// | `session_id` | `string` | Yes | Session to stop |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct StopRequest {
    /// The session whose generation should be cancelled.
    pub session_id: String,
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
    app: tauri::AppHandle,
    bridge: tauri::State<'_, PythonBridge>,
) -> Result<Vec<ChatChunk>, FrontendError> {
    tracing::info!(session_id = %request.session_id, "agent_chat called");

    let message_id = Uuid::new_v4().to_string();
    let session_id = request.session_id.clone();

    // 1. Emit stream start event
    let _ = app.emit(
        AGENT_STREAM_START,
        AgentStreamStart {
            session_id: session_id.clone(),
            message_id: message_id.clone(),
        },
    );

    // 2. Build Python backend request body
    let body = serde_json::json!({
        "session_id": request.session_id,
        "multi_modal_message": {
            "text": request.text.unwrap_or_default(),
            "image_base64_list": request.image_base64_list,
        }
    });

    // 3. Send SSE request to Python backend
    let resp = match bridge.post_sse("/sessions/agent/sse", &body).await {
        Ok(r) => r,
        Err(e) => {
            let fe: FrontendError = e.into();
            let _ = app.emit(
                AGENT_STREAM_ERROR,
                AgentStreamError {
                    session_id: session_id.clone(),
                    message_id: message_id.clone(),
                    code: fe.code.clone(),
                    message: fe.message.clone(),
                },
            );
            return Err(fe);
        }
    };

    // 4. Consume SSE stream, emit chunks as Tauri events
    let stream = match PythonBridge::sse_lines(resp).await {
        Ok(s) => s,
        Err(e) => {
            let fe: FrontendError = e.into();
            let _ = app.emit(
                AGENT_STREAM_ERROR,
                AgentStreamError {
                    session_id: session_id.clone(),
                    message_id: message_id.clone(),
                    code: fe.code.clone(),
                    message: fe.message.clone(),
                },
            );
            return Err(fe);
        }
    };

    let mut chunks: Vec<ChatChunk> = Vec::new();
    let mut stream = std::pin::pin!(stream);

    while let Some(line) = stream.next().await {
        let chunk = ChatChunk {
            content: line,
            done: false,
        };
        let _ = app.emit(
            AGENT_STREAM_CHUNK,
            AgentStreamChunk {
                session_id: session_id.clone(),
                message_id: message_id.clone(),
                content: chunk.content.clone(),
            },
        );
        chunks.push(chunk);
    }

    // 5. Mark the last chunk as done
    if let Some(last) = chunks.last_mut() {
        last.done = true;
    }

    // 6. Emit stream end event
    let _ = app.emit(
        AGENT_STREAM_END,
        AgentStreamEnd {
            session_id,
            message_id,
            total_chunks: chunks.len() as u32,
        },
    );

    Ok(chunks)
}

/// Stop an ongoing agent generation for the given session.
///
/// Sends a cancellation request to the Python backend. The SSE
/// stream will terminate and emit `agent:stream:end`.
///
/// # Frontend Example
///
/// ```typescript
/// await invoke('agent_stop', { request: { session_id: 'main' } });
/// ```
#[tauri::command]
pub async fn agent_stop(
    request: StopRequest,
    bridge: tauri::State<'_, PythonBridge>,
) -> Result<(), FrontendError> {
    tracing::info!(session_id = %request.session_id, "agent_stop called");
    bridge.post_stop(&request.session_id).await;
    Ok(())
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
