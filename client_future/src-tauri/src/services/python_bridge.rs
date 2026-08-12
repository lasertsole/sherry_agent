//! HTTP bridge to the Python backend (Robyn server).
//!
//! Provides typed REST methods and SSE streaming that map to
//! the Python backend's HTTP endpoints. All business logic lives
//! in Python; this module only handles transport.
//!
//! # Error Handling
//!
//! Python backend errors use the format:
//! ```json
//! {"success": false, "message": "Internal Server Error", "error": "..."}
//! ```
//! These are mapped to [`AppError::Backend`] and then to [`FrontendError`].

use base64::Engine;
use crate::utils::error::{AppError, AppResult};
use futures_util::StreamExt;
use reqwest::Client;
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::time::Duration;

// ── Python backend error response ─────────────────────────

/// Error body returned by the Python backend on failure.
#[derive(Debug, serde::Deserialize)]
struct PythonErrorResponse {
    #[allow(dead_code)]
    success: Option<bool>,
    message: Option<String>,
    error: Option<String>,
}

impl PythonErrorResponse {
    /// Extract the most descriptive error message available.
    fn into_message(self) -> String {
        self.error
            .or(self.message)
            .unwrap_or_else(|| "unknown backend error".to_string())
    }
}

// ── Bridge ────────────────────────────────────────────────

/// HTTP bridge to the Python backend.
///
/// Manages a `reqwest::Client` connection pool and provides
/// typed methods for every Python REST endpoint.
pub struct PythonBridge {
    client: Client,
    base_url: String,
}

impl PythonBridge {
    /// Create a new bridge with the given backend URL and timeout.
    pub fn new(base_url: String, timeout_secs: u64) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .pool_max_idle_per_host(10)
            .build()
            .expect("failed to build reqwest client");

        // Strip trailing slash for consistent URL joining.
        let base_url = base_url.trim_end_matches('/').to_string();

        tracing::info!(base_url = %base_url, timeout_secs, "PythonBridge initialized");

        Self { client, base_url }
    }

    /// Build a full URL by appending a path to the base URL.
    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// Inspect a response for Python backend errors and extract the error message.
    async fn check_response(resp: reqwest::Response) -> AppResult<reqwest::Response> {
        if resp.status().is_success() {
            return Ok(resp);
        }

        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();

        // Try to parse as Python error JSON.
        let message = if let Ok(err_resp) = serde_json::from_str::<PythonErrorResponse>(&body) {
            err_resp.into_message()
        } else {
            body
        };

        Err(AppError::Backend(format!("HTTP {status}: {message}")))
    }

    /// Upload each base64-encoded image to the backend `/images/upload`
    /// endpoint, returning the corresponding list of absolute HTTP URLs
    /// (same order).
    ///
    /// The backend stores raw bytes and serves them from `/images/{filename}`.
    /// Returns URLs to place in `image_path_list`.
    pub async fn upload_images(&self, base64_list: &[String]) -> AppResult<Vec<String>> {
        if base64_list.is_empty() {
            return Ok(Vec::new());
        }

        let mut urls = Vec::with_capacity(base64_list.len());

        for b64 in base64_list {
            let encoded = if let Some(comma_pos) = b64.find(',') {
                &b64[comma_pos + 1..]
            } else {
                b64.as_str()
            };

            let bytes = base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .map_err(|e| AppError::Backend(format!("base64 decode failed: {e}")))?;

            let resp = self
                .client
                .post(self.url("/images/upload"))
                .header("Content-Type", "image/png")
                .body(bytes)
                .send()
                .await?;

            let resp = Self::check_response(resp).await?;
            let body = resp.bytes().await?;
            let json: serde_json::Value = serde_json::from_slice(&body)
                .map_err(|e| AppError::Backend(format!("invalid upload response: {e}")))?;

            let url = json
                .get("url")
                .and_then(|v| v.as_str())
                .map(String::from)
                .ok_or_else(|| AppError::Backend("missing 'url' in upload response".into()))?;

            tracing::debug!(image_url = %url, "image uploaded");
            urls.push(url);
        }

        tracing::info!(count = urls.len(), "uploaded images to backend");
        Ok(urls)
    }

    // ── JSON REST methods ────────────────────────────────────

    /// `GET {path}` with optional query parameters, returning deserialized JSON.
    pub async fn get_json<T: DeserializeOwned>(
        &self,
        path: &str,
        query: &[(&str, &str)],
    ) -> AppResult<T> {
        let resp = self
            .client
            .get(self.url(path))
            .query(query)
            .send()
            .await?;

        let resp = Self::check_response(resp).await?;
        resp.json::<T>().await.map_err(|e| {
            AppError::Backend(format!("failed to deserialize GET {path}: {e}"))
        })
    }

    /// `POST {path}` with a JSON body, returning deserialized JSON.
    pub async fn post_json<B: Serialize, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> AppResult<T> {
        let resp = self
            .client
            .post(self.url(path))
            .json(body)
            .send()
            .await?;

        let resp = Self::check_response(resp).await?;
        resp.json::<T>().await.map_err(|e| {
            AppError::Backend(format!("failed to deserialize POST {path}: {e}"))
        })
    }

    /// `PUT {path}` with a JSON body, returning deserialized JSON.
    pub async fn put_json<B: Serialize, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> AppResult<T> {
        let resp = self
            .client
            .put(self.url(path))
            .json(body)
            .send()
            .await?;

        let resp = Self::check_response(resp).await?;
        resp.json::<T>().await.map_err(|e| {
            AppError::Backend(format!("failed to deserialize PUT {path}: {e}"))
        })
    }

    /// `PATCH {path}` with a JSON body, returning deserialized JSON.
    pub async fn patch_json<B: Serialize, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> AppResult<T> {
        let resp = self
            .client
            .patch(self.url(path))
            .json(body)
            .send()
            .await?;

        let resp = Self::check_response(resp).await?;
        resp.json::<T>().await.map_err(|e| {
            AppError::Backend(format!("failed to deserialize PATCH {path}: {e}"))
        })
    }

    /// `DELETE {path}` with a JSON body. Returns `()` on success.
    pub async fn delete_json<B: Serialize>(
        &self,
        path: &str,
        body: &B,
    ) -> AppResult<()> {
        let resp = self
            .client
            .delete(self.url(path))
            .json(body)
            .send()
            .await?;

        Self::check_response(resp).await?;
        Ok(())
    }

    /// `POST {path}` with a JSON body, returning the SSE stream as raw bytes.
    ///
    /// The caller is responsible for parsing SSE `data:` lines and
    /// forwarding them as Tauri events.
    ///
    /// Returns `Ok(reqwest::Response)` with the body not yet consumed.
    pub async fn post_sse(
        &self,
        path: &str,
        body: &serde_json::Value,
    ) -> AppResult<reqwest::Response> {
        let resp = self
            .client
            .post(self.url(path))
            .json(body)
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(AppError::Backend(format!("SSE request failed: HTTP {status}: {text}")));
        }

        Ok(resp)
    }

    /// Consume a SSE response stream, yielding each `data:` line as a `String`.
    ///
    /// The returned stream strips the SSE protocol framing (`data: ` prefix,
    /// double-newline delimiters) and yields raw content strings.
    pub async fn sse_lines(
        resp: reqwest::Response,
    ) -> AppResult<impl futures_util::Stream<Item = String>> {
        let stream = resp.bytes_stream();

        // Buffer for accumulating partial lines across chunks.
        let mut buffer = String::new();

        let line_stream = stream.flat_map(move |chunk_result| {
            let chunk = match chunk_result {
                Ok(bytes) => bytes,
                Err(e) => {
                    tracing::warn!("SSE chunk read error: {e}");
                    return futures_util::stream::iter(vec![]);
                }
            };

            buffer.push_str(&String::from_utf8_lossy(&chunk));

            let mut lines = Vec::new();
            while let Some(pos) = buffer.find('\n') {
                let line = buffer[..pos].trim_end_matches('\r').to_string();
                buffer = buffer[pos + 1..].to_string();

                // SSE data line: "data: content"
                if let Some(data) = line.strip_prefix("data: ") {
                    lines.push(data.to_string());
                } else if line == "data:" {
                    // Empty data line (keep-alive or empty chunk)
                    lines.push(String::new());
                }
                // Ignore comment lines (": ...") and field lines ("event:", "id:", etc.)
            }

            futures_util::stream::iter(lines)
        });

        Ok(line_stream)
    }

    /// Build the agent WebSocket URL by converting `base_url`
    /// (`http(s)://...`) into its `ws(s)://...` counterpart.
    fn ws_url(&self) -> String {
        let ws_base = if let Some(rest) = self.base_url.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = self.base_url.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            self.base_url.clone()
        };
        format!("{}", ws_base.trim_end_matches('/'))
    }

    /// Post a stop-generation request to the Python backend over WebSocket.
    ///
    /// Connects to `/sessions/agent/ws`, sends `{"type":"stop","session_id":...}`,
    /// and waits for the `{"event":"stopped"}` acknowledgement. This is the
    /// transport-level replacement of the legacy HTTP `/sessions/agent/sse/stop`
    /// endpoint. Errors are logged but not propagated because the stream may
    /// already be closing; the call never blocks indefinitely.
    pub async fn post_stop(&self, session_id: &str) {
        let url = format!("{}/sessions/agent/ws", self.ws_url());
        // Wait a short time for the "stopped" acknowledgement; a timeout is
        // acceptable since the stream may already be closing.
        let ws_result = tokio::time::timeout(
            Duration::from_secs(5),
            PythonBridge::send_stop_ws(&url, session_id),
        )
        .await;

        match ws_result {
            Ok(Ok(())) => tracing::info!(session_id, "stop generation request sent via WebSocket"),
            Ok(Err(e)) => tracing::warn!(session_id, error = %e, "stop request failed via WebSocket"),
            Err(_) => tracing::warn!(session_id, "stop request timed out via WebSocket"),
        }
    }

    /// Open a WebSocket to the given agent URL, send the stop frame, and wait
    /// for the `{"event":"stopped"}` acknowledgement.
    async fn send_stop_ws(url: &str, session_id: &str) -> Result<(), String> {
        use futures_util::SinkExt;

        let (mut socket, _) =
            tokio_tungstenite::connect_async(url).await.map_err(|e| e.to_string())?;
        socket
            .send(tokio_tungstenite::tungstenite::Message::Text(
                serde_json::json!({ "type": "stop", "session_id": session_id }).to_string(),
            ))
            .await
            .map_err(|e| e.to_string())?;

        loop {
            match socket.next().await {
                Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                    if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&text) {
                        if obj.get("event").and_then(|v| v.as_str()) == Some("stopped") {
                            return Ok(());
                        }
                    }
                }
                Some(Ok(_)) => {}
                Some(Err(e)) => return Err(e.to_string()),
                None => return Err("connection closed before stop confirmation".into()),
            }
        }
    }

    /// Stream an agent chat turn over the agent WebSocket.
    ///
    /// Connects to `{path}` (typically `/sessions/agent/ws`), sends `body`
    /// (the [`ChatRequest`] payload: `session_id` + `multi_modal_message`)
    /// once, then drives the receive loop:
    ///
    /// | server event | action |
    /// |--------------|--------|
    /// | `chunk` | forwards `content` to `on_chunk` |
    /// | `error` | returns `Err(AppError::Backend)` with the `content` message |
    /// | `done` / `stopped` | returns `Ok(())` |
    ///
    /// This is the transport-level replacement of the legacy HTTP
    /// `POST /sessions/agent/sse` endpoint, which no longer exists in the
    /// Python backend. Keep this method generic over a path so future agent
    /// channels can reuse it.
    ///
    /// [`ChatRequest`]: crate::commands::agent::ChatRequest
    pub async fn stream_agent_message<F>(
        &self,
        path: &str,
        body: &serde_json::Value,
        mut on_chunk: F,
    ) -> AppResult<()>
    where
        F: FnMut(&str, &str),
    {
        use futures_util::SinkExt;

        let url = format!("{}{}", self.ws_url(), path);
        let (mut socket, _) = tokio_tungstenite::connect_async(&url)
            .await
            .map_err(|e| AppError::Backend(format!("agent WS connect failed: {e}")))?;

        socket
            .send(tokio_tungstenite::tungstenite::Message::Text(body.to_string()))
            .await
            .map_err(|e| AppError::Backend(format!("agent WS send failed: {e}")))?;

        loop {
            match socket.next().await {
                Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                    let obj: serde_json::Value = match serde_json::from_str(&text) {
                        Ok(v) => v,
                        Err(e) => {
                            tracing::warn!("agent WS: unparseable frame: {e}");
                            continue;
                        }
                    };

                    match obj.get("event").and_then(|v| v.as_str()).unwrap_or("") {
                        "chunk" => {
                            let content = obj.get("content").and_then(|v| v.as_str()).unwrap_or("");
                            let chunk_type = obj.get("type").and_then(|v| v.as_str()).unwrap_or("text");
                            on_chunk(content, chunk_type);
                        }
                        "error" => {
                            let msg = obj
                                .get("content")
                                .and_then(|v| v.as_str())
                                .unwrap_or("agent error");
                            return Err(AppError::Backend(msg.to_string()));
                        }
                        "done" | "stopped" => return Ok(()),
                        _ => {}
                    }
                }
                Some(Ok(_)) => {}
                Some(Err(e)) => {
                    return Err(AppError::Backend(format!("agent WS read error: {e}")));
                }
                None => return Err(AppError::Backend("agent WS closed unexpectedly".into())),
            }
        }
    }
}

// ── Tests ─────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bridge_url_construction() {
        let bridge = PythonBridge::new("http://127.0.0.1:8080".into(), 30);
        assert_eq!(bridge.url("/sessions"), "http://127.0.0.1:8080/sessions");
        assert_eq!(
            bridge.url("/system_prompt"),
            "http://127.0.0.1:8080/system_prompt"
        );
    }

    #[test]
    fn bridge_url_strips_trailing_slash() {
        let bridge = PythonBridge::new("http://127.0.0.1:8080/".into(), 30);
        assert_eq!(bridge.url("/sessions"), "http://127.0.0.1:8080/sessions");
    }

    #[test]
    fn python_error_response_extracts_error_field() {
        let json = r#"{"success": false, "message": "Internal Server Error", "error": "division by zero"}"#;
        let err: PythonErrorResponse = serde_json::from_str(json).unwrap();
        assert_eq!(err.into_message(), "division by zero");
    }

    #[test]
    fn python_error_response_falls_back_to_message() {
        let json = r#"{"success": false, "message": "something went wrong"}"#;
        let err: PythonErrorResponse = serde_json::from_str(json).unwrap();
        assert_eq!(err.into_message(), "something went wrong");
    }

    #[test]
    fn python_error_response_default_message() {
        let json = r#"{"success": false}"#;
        let err: PythonErrorResponse = serde_json::from_str(json).unwrap();
        assert_eq!(err.into_message(), "unknown backend error");
    }
}
