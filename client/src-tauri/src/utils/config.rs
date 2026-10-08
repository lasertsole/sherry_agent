//! Application configuration for the Tauri backend.
//!
//! Reads settings from environment variables and provides defaults
//! for the Python backend bridge.

use serde::{Deserialize, Serialize};

/// Default Python backend URL when no environment variable is set.
const DEFAULT_BACKEND_URL: &str = "http://127.0.0.1:8080";

/// Default request timeout in seconds (2 minutes).
const DEFAULT_TIMEOUT_SECS: u64 = 120;

/// Application-level configuration, managed as Tauri State.
///
/// | Field | Env Variable | Default |
/// |-------|-------------|---------|
/// | `python_backend_url` | `VITE_API_BACK_URL` | `http://127.0.0.1:8080` |
/// | `python_backend_timeout_secs` | `BACKEND_TIMEOUT_SECS` | `120` |
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    /// Base URL of the Python backend (e.g. `http://127.0.0.1:8080`).
    pub python_backend_url: String,
    /// HTTP request timeout in seconds for non-streaming calls.
    pub python_backend_timeout_secs: u64,
}

impl AppConfig {
    /// Load configuration from environment variables.
    ///
    /// - `VITE_API_BACK_URL` — Python backend base URL (shared with Nuxt frontend).
    /// - `BACKEND_TIMEOUT_SECS` — request timeout override.
    pub fn from_env() -> Self {
        let python_backend_url = std::env::var("VITE_API_BACK_URL")
            .unwrap_or_else(|_| DEFAULT_BACKEND_URL.to_string());

        let python_backend_timeout_secs = std::env::var("BACKEND_TIMEOUT_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_TIMEOUT_SECS);

        Self {
            python_backend_url,
            python_backend_timeout_secs,
        }
    }
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            python_backend_url: DEFAULT_BACKEND_URL.to_string(),
            python_backend_timeout_secs: DEFAULT_TIMEOUT_SECS,
        }
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_has_correct_url() {
        let config = AppConfig::default();
        assert_eq!(config.python_backend_url, "http://127.0.0.1:8080");
    }

    #[test]
    fn default_config_has_correct_timeout() {
        let config = AppConfig::default();
        assert_eq!(config.python_backend_timeout_secs, 120);
    }

    #[test]
    fn config_serializes_to_json() {
        let config = AppConfig::default();
        let json = serde_json::to_string(&config).unwrap();
        assert!(json.contains("python_backend_url"));
        assert!(json.contains("127.0.0.1:8080"));
    }

    #[test]
    fn config_deserializes_from_json() {
        let json = r#"{"python_backend_url":"http://localhost:9000","python_backend_timeout_secs":60}"#;
        let config: AppConfig = serde_json::from_str(json).unwrap();
        assert_eq!(config.python_backend_url, "http://localhost:9000");
        assert_eq!(config.python_backend_timeout_secs, 60);
    }

    #[test]
    fn config_round_trip() {
        let original = AppConfig {
            python_backend_url: "http://custom:1234".into(),
            python_backend_timeout_secs: 30,
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: AppConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.python_backend_url, original.python_backend_url);
        assert_eq!(
            deserialized.python_backend_timeout_secs,
            original.python_backend_timeout_secs
        );
    }
}
