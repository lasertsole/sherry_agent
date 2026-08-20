//! Agent IPC commands — chat, streaming, and agent lifecycle.
//!
//! Maps to Python backend:
//! - WebSocket `/sessions/agent/ws` → [`agent_chat`]
//! - WebSocket `/sessions/agent/ws` (`{"type":"stop"}`) → [`agent_stop`]
//!
//! # Streaming Events
//!
//! The agent uses Tauri events for streaming responses. See [`super::events`]
//! for the complete event lifecycle.

use super::events::*;
use crate::services::python_bridge::{PythonBridge, UploadKind};
use crate::utils::error::FrontendError;
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
/// | `image_base64_list` | `string[]` | No | Base64-encoded image data (uploaded to backend before WS call) |
/// | `image_path_list` | `string[]` | No | HTTP image URLs already uploaded to the backend |
/// | `audio_base64_list` | `string[]` | No | Base64-encoded audio data (uploaded to backend before WS call) |
/// | `audio_path_list` | `string[]` | No | HTTP audio URLs already uploaded to the backend |
/// | `video_base64_list` | `string[]` | No | Base64-encoded video data (uploaded to backend before WS call) |
/// | `video_path_list` | `string[]` | No | HTTP video URLs already uploaded to the backend |
///
/// At least one of `text` or the media lists should be provided.
///
/// # Example
///
/// ```json
/// {
///   "session_id": "default",
///   "text": "Hello, how are you?",
///   "image_base64_list": [],
///   "image_path_list": [],
///   "audio_base64_list": [],
///   "audio_path_list": [],
///   "video_base64_list": [],
///   "video_path_list": []
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
    /// Optional list of HTTP image URLs already uploaded to the backend.
    #[serde(default)]
    pub image_path_list: Vec<String>,
    /// Optional list of base64-encoded audio for multi-modal input.
    #[serde(default)]
    pub audio_base64_list: Vec<String>,
    /// Optional list of HTTP audio URLs already uploaded to the backend.
    #[serde(default)]
    pub audio_path_list: Vec<String>,
    /// Optional list of base64-encoded video for multi-modal input.
    #[serde(default)]
    pub video_base64_list: Vec<String>,
    /// Optional list of HTTP video URLs already uploaded to the backend.
    #[serde(default)]
    pub video_path_list: Vec<String>,
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
///     session_id: 'default',
///     text: 'Hello!',
///     image_base64_list: [],
///   },
/// });
///
/// // Multi-modal chat with images
/// const chunks = await invoke<ChatChunk[]>('agent_chat', {
///   request: {
///     session_id: 'default',
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

    // 1. Upload any base64 images/audio/video to the backend over HTTP,
    //    converting them to lightweight URLs so the WebSocket text frame stays
    //    small.
    let image_urls = bridge
        .upload_media(UploadKind::Image, &request.image_base64_list)
        .await?;
    let audio_urls = bridge
        .upload_media(UploadKind::Audio, &request.audio_base64_list)
        .await?;
    let video_urls = bridge
        .upload_media(UploadKind::Video, &request.video_base64_list)
        .await?;

    // 2. Emit stream start event (only after upload succeeds)
    let _ = app.emit(
        AGENT_STREAM_START,
        AgentStreamStart {
            session_id: session_id.clone(),
            message_id: message_id.clone(),
        },
    );

    // 3. Build Python backend request body with media URLs (not base64)
    let body = serde_json::json!({
        "session_id": &request.session_id,
        "multi_modal_message": {
            "text": request.text.unwrap_or_default(),
            "image_base64_list": [],
            "image_path_list": image_urls,
            "audio_base64_list": [],
            "audio_path_list": audio_urls,
            "video_base64_list": [],
            "video_path_list": video_urls,
        }
    });

    // 4. Stream the turn over the agent WebSocket, emitting chunks as Tauri
    //    events in real time. This replaces the legacy SSE path
    //    (`POST /sessions/agent/sse`), which no longer exists in the backend.
    let mut chunks: Vec<ChatChunk> = Vec::new();
    let stream_result = bridge
        .stream_agent_message("/sessions/agent/ws", &body, |content, chunk_type| {
            let _ = app.emit(
                AGENT_STREAM_CHUNK,
                AgentStreamChunk {
                    session_id: session_id.clone(),
                    message_id: message_id.clone(),
                    content: content.to_string(),
                    chunk_type: chunk_type.to_string(),
                },
            );
            chunks.push(ChatChunk {
                content: content.to_string(),
                done: false,
            });
        })
        .await;

    if let Err(e) = stream_result {
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

    // 4. Mark the last chunk as done
    if let Some(last) = chunks.last_mut() {
        last.done = true;
    }

    // 5. Emit stream end event
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
/// Sends a cancellation request to the Python backend over the agent
/// WebSocket (`/sessions/agent/ws`, `{"type":"stop"}`). The SSE
/// stream will terminate and emit `agent:stream:end`.
///
/// # Frontend Example
///
/// ```typescript
/// await invoke('agent_stop', { request: { session_id: 'default' } });
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
        assert!(req.image_path_list.is_empty());
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
    fn chat_request_deserializes_image_path_list() {
        let json = r#"{"session_id":"s1","image_path_list":["http://127.0.0.1:8080/images/abc.png"]}"#;
        let req: ChatRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.image_path_list.len(), 1);
        assert_eq!(req.image_path_list[0], "http://127.0.0.1:8080/images/abc.png");
    }

    #[test]
    fn chat_request_round_trip() {
        let original = ChatRequest {
            session_id: "default".into(),
            text: Some("How's the weather?".into()),
            image_base64_list: vec![],
            image_path_list: vec![],
            audio_base64_list: vec![],
            audio_path_list: vec![],
            video_base64_list: vec![],
            video_path_list: vec![],
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
