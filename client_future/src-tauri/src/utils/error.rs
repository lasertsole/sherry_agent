//! Unified error handling for EMA AI Agent.
//!
//! ## Design
//!
//! - [`AppError`] — top-level error enum covering all domains.
//!   Uses `thiserror` for structured `Display` / `Error` impls.
//! - [`AppResult<T>`] — convenience alias for `Result<T, AppError>`.
//! - [`FrontendError`] — JSON-serializable error for Tauri IPC responses.
//!
//! ## Usage
//!
//! ```rust,ignore
//! use crate::utils::error::{AppError, AppResult};
//!
//! fn load_config(path: &str) -> AppResult<Config> {
//!     let content = std::fs::read_to_string(path)
//!         .map_err(|e| AppError::Io(e))?;
//!     Ok(toml::from_str(&content)?)
//! }
//! ```
//!
//! In Tauri commands, convert to [`FrontendError`] automatically:
//!
//! ```rust,ignore
//! #[tauri::command]
//! async fn get_session(id: String) -> Result<Session, FrontendError> {
//!     let session = session_store::get(&id).await?;  // AppError → FrontendError
//!     Ok(session)
//! }
//! ```

use serde::{Deserialize, Serialize};
use std::fmt;
use ts_rs::TS;

// ────────────────────────────────────────────────────────────
// Top-level application error
// ────────────────────────────────────────────────────────────

/// Unified error type for the entire application.
///
/// Each variant maps to a specific domain so that callers can
/// match on the error kind and react accordingly.
#[derive(Debug, thiserror::Error)]
pub enum AppError {
    // ── Infrastructure ────────────────────────────────────────
    /// Configuration loading or parsing failure.
    #[error("config error: {0}")]
    Config(String),

    /// Database operation failure (SQLite, FTS5, migrations).
    #[error("database error: {0}")]
    Database(String),

    /// File-system or generic I/O failure.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    // ── LLM / Agent ──────────────────────────────────────────
    /// LLM API call failure (DeepSeek, Ollama, OpenAI, etc.).
    #[error("model error: {0}")]
    Model(String),

    /// Agent execution failure (LangGraph, tool loop, etc.).
    #[error("agent error: {0}")]
    Agent(String),

    /// RAG pipeline failure (parsing, embedding, retrieval).
    #[error("rag error: {0}")]
    Rag(String),

    // ── Communication ─────────────────────────────────────────
    /// Channel error (QQ bot, WebSocket, message bus).
    #[error("channel error: {0}")]
    Channel(String),

    /// Session management failure.
    #[error("session error: {0}")]
    Session(String),

    // ── Tools & Skills ────────────────────────────────────────
    /// Tool execution failure (Python REPL, terminal, file ops).
    #[error("tool error: {0}")]
    Tool(String),

    /// Skill loading or registration failure.
    #[error("skill error: {0}")]
    Skill(String),

    // ── Catch-all ─────────────────────────────────────────────
    /// Unexpected error that does not fit any other category.
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

/// Convenience alias: `Result<T, AppError>`.
pub type AppResult<T> = Result<T, AppError>;

// ────────────────────────────────────────────────────────────
// Error code for each variant
// ────────────────────────────────────────────────────────────

impl AppError {
    /// Returns a stable, machine-readable error code.
    ///
    /// Frontend code can match on these strings to show
    /// localized messages or trigger specific UI flows.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Config(_) => "CONFIG_ERROR",
            Self::Database(_) => "DATABASE_ERROR",
            Self::Io(_) => "IO_ERROR",
            Self::Model(_) => "MODEL_ERROR",
            Self::Agent(_) => "AGENT_ERROR",
            Self::Rag(_) => "RAG_ERROR",
            Self::Channel(_) => "CHANNEL_ERROR",
            Self::Session(_) => "SESSION_ERROR",
            Self::Tool(_) => "TOOL_ERROR",
            Self::Skill(_) => "SKILL_ERROR",
            Self::Other(_) => "UNKNOWN_ERROR",
        }
    }

    /// Whether this error is worth retrying.
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::Io(_) | Self::Channel(_) | Self::Model(_) | Self::Database(_)
        )
    }
}

// ────────────────────────────────────────────────────────────
// Frontend-facing error (Tauri IPC)
// ────────────────────────────────────────────────────────────

/// JSON-serializable error sent to the frontend via Tauri IPC.
///
/// Designed to be both human-readable and machine-parseable:
///
/// ```json
/// {
///   "code": "MODEL_ERROR",
///   "message": "connection refused: http://localhost:11434"
/// }
/// ```
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct FrontendError {
    /// Stable error code (e.g. `"MODEL_ERROR"`).
    pub code: String,
    /// Human-readable description.
    pub message: String,
}

impl fmt::Display for FrontendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

/// Automatic conversion: [`AppError`] → [`FrontendError`].
impl From<AppError> for FrontendError {
    fn from(err: AppError) -> Self {
        // Log the full error chain at ERROR level.
        tracing::error!(code = err.code(), "{err:#}");

        Self {
            code: err.code().to_owned(),
            message: err.to_string(),
        }
    }
}

/// Allow Tauri commands to return `Result<T, FrontendError>` directly.
impl From<FrontendError> for tauri::Error {
    fn from(err: FrontendError) -> Self {
        tauri::Error::Anyhow(anyhow::anyhow!("{err}"))
    }
}

// ────────────────────────────────────────────────────────────
// Convenience conversions from common third-party errors
// ────────────────────────────────────────────────────────────

impl From<serde_json::Error> for AppError {
    fn from(err: serde_json::Error) -> Self {
        Self::Config(err.to_string())
    }
}

// ────────────────────────────────────────────────────────────
// Macros for ergonomic error construction
// ────────────────────────────────────────────────────────────

/// Shorthand for creating an [`AppError::Config`].
///
/// ```rust,ignore
/// config_err!("missing field: {}", field_name);
/// ```
#[macro_export]
macro_rules! config_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Config(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Model`].
#[macro_export]
macro_rules! model_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Model(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Tool`].
#[macro_export]
macro_rules! tool_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Tool(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Channel`].
#[macro_export]
macro_rules! channel_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Channel(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Session`].
#[macro_export]
macro_rules! session_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Session(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Agent`].
#[macro_export]
macro_rules! agent_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Agent(format!($($arg)*))
    };
}

/// Shorthand for creating an [`AppError::Database`].
#[macro_export]
macro_rules! db_err {
    ($($arg:tt)*) => {
        $crate::utils::error::AppError::Database(format!($($arg)*))
    };
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // -- Error code tests --

    #[test]
    fn error_codes_are_stable() {
        assert_eq!(AppError::Config("x".into()).code(), "CONFIG_ERROR");
        assert_eq!(AppError::Database("x".into()).code(), "DATABASE_ERROR");
        assert_eq!(AppError::Model("x".into()).code(), "MODEL_ERROR");
        assert_eq!(AppError::Agent("x".into()).code(), "AGENT_ERROR");
        assert_eq!(AppError::Rag("x".into()).code(), "RAG_ERROR");
        assert_eq!(AppError::Channel("x".into()).code(), "CHANNEL_ERROR");
        assert_eq!(AppError::Session("x".into()).code(), "SESSION_ERROR");
        assert_eq!(AppError::Tool("x".into()).code(), "TOOL_ERROR");
        assert_eq!(AppError::Skill("x".into()).code(), "SKILL_ERROR");
        assert_eq!(
            AppError::Other(anyhow::anyhow!("x")).code(),
            "UNKNOWN_ERROR"
        );
    }

    #[test]
    fn io_error_code_is_correct() {
        let err = AppError::Io(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "file not found",
        ));
        assert_eq!(err.code(), "IO_ERROR");
    }

    // -- Retryable tests --

    #[test]
    fn retryable_errors() {
        assert!(AppError::Io(std::io::Error::new(
            std::io::ErrorKind::Other,
            "x"
        ))
        .is_retryable());
        assert!(AppError::Channel("timeout".into()).is_retryable());
        assert!(AppError::Model("connection refused".into()).is_retryable());
        assert!(AppError::Database("locked".into()).is_retryable());
    }

    #[test]
    fn non_retryable_errors() {
        assert!(!AppError::Config("bad config".into()).is_retryable());
        assert!(!AppError::Agent("tool loop".into()).is_retryable());
        assert!(!AppError::Session("not found".into()).is_retryable());
        assert!(!AppError::Tool("syntax error".into()).is_retryable());
        assert!(!AppError::Skill("not found".into()).is_retryable());
        assert!(!AppError::Rag("parse error".into()).is_retryable());
    }

    // -- Display tests --

    #[test]
    fn error_display_messages() {
        let err = AppError::Config("missing key".into());
        assert_eq!(err.to_string(), "config error: missing key");

        let err = AppError::Model("timeout".into());
        assert_eq!(err.to_string(), "model error: timeout");
    }

    // -- FrontendError conversion tests --

    #[test]
    fn app_error_converts_to_frontend_error() {
        let app_err = AppError::Model("connection refused".into());
        let fe_err: FrontendError = app_err.into();
        assert_eq!(fe_err.code, "MODEL_ERROR");
        assert_eq!(fe_err.message, "model error: connection refused");
    }

    #[test]
    fn frontend_error_serializes_to_json() {
        let fe_err = FrontendError {
            code: "TOOL_ERROR".into(),
            message: "tool error: python REPL crashed".into(),
        };
        let json = serde_json::to_string(&fe_err).unwrap();
        assert!(json.contains("\"code\":\"TOOL_ERROR\""));
        assert!(json.contains("python REPL crashed"));
    }

    #[test]
    fn frontend_error_display() {
        let fe_err = FrontendError {
            code: "AGENT_ERROR".into(),
            message: "agent error: tool loop detected".into(),
        };
        assert_eq!(
            fe_err.to_string(),
            "[AGENT_ERROR] agent error: tool loop detected"
        );
    }

    #[test]
    fn frontend_error_round_trip() {
        let original = FrontendError {
            code: "SESSION_ERROR".into(),
            message: "session error: not found".into(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: FrontendError = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.code, original.code);
        assert_eq!(deserialized.message, original.message);
    }

    // -- serde_json conversion test --

    #[test]
    fn serde_json_error_converts_to_config() {
        let json_err: serde_json::Error =
            serde_json::from_str::<String>("not json").unwrap_err();
        let app_err: AppError = json_err.into();
        assert_eq!(app_err.code(), "CONFIG_ERROR");
    }

    // -- Macro tests --

    #[test]
    fn error_macros_create_correct_variants() {
        let err = config_err!("missing: {}", "key");
        assert!(matches!(err, AppError::Config(msg) if msg == "missing: key"));

        let err = model_err!("timeout after {}s", 30);
        assert!(matches!(err, AppError::Model(msg) if msg == "timeout after 30s"));

        let err = tool_err!("crashed");
        assert!(matches!(err, AppError::Tool(msg) if msg == "crashed"));

        let err = channel_err!("disconnected");
        assert!(matches!(err, AppError::Channel(msg) if msg == "disconnected"));

        let err = session_err!("expired");
        assert!(matches!(err, AppError::Session(msg) if msg == "expired"));

        let err = agent_err!("loop");
        assert!(matches!(err, AppError::Agent(msg) if msg == "loop"));

        let err = db_err!("locked");
        assert!(matches!(err, AppError::Database(msg) if msg == "locked"));
    }
}
