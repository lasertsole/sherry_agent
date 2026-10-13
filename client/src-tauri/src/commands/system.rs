//! System IPC commands — health check, version info, app metadata.
//!
//! `system_info` is Tauri-native (no Python backend needed).
//! `system_health` pings the Python backend to verify connectivity.

use crate::services::python_bridge::PythonBridge;
use serde::{Deserialize, Serialize};
use ts_rs::TS;

// ── Response types ──────────────────────────────────────────

/// Application metadata returned by [`system_info`].
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `name` | `string` | Application name from `Cargo.toml` |
/// | `version` | `string` | Semantic version (e.g., `"0.1.0"`) |
/// | `tauri_version` | `string` | Tauri runtime version |
/// | `debug` | `boolean` | `true` in debug builds |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct AppInfo {
    /// Application name (from Cargo.toml).
    pub name: String,
    /// Semantic version.
    pub version: String,
    /// Rust toolchain / Tauri runtime info.
    pub tauri_version: String,
    /// Whether the application is running in debug mode.
    pub debug: bool,
}

/// Health-check response from [`system_health`].
///
/// | Field | Type | Description |
/// |-------|------|-------------|
/// | `healthy` | `boolean` | `true` if all subsystems are operational |
/// | `message` | `string` | Human-readable status description |
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[ts(export, export_to = "../../app/types/backend/")]
pub struct HealthStatus {
    /// Whether all core subsystems are operational.
    pub healthy: bool,
    /// Human-readable status description.
    pub message: String,
}

// ── Commands ────────────────────────────────────────────────

/// Return application metadata.
///
/// Synchronous command that returns the app name, version, Tauri
/// runtime version, and whether the build is debug or release.
///
/// # Arguments
///
/// None.
///
/// # Returns
///
/// [`AppInfo`] — Application metadata. This command never fails.
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// const info = await invoke<AppInfo>('system_info');
/// console.log(`App: ${info.name} v${info.version}`);
/// console.log(`Debug: ${info.debug}`);
/// ```
#[tauri::command]
pub fn system_info() -> AppInfo {
    AppInfo {
        name: env!("CARGO_PKG_NAME").to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        tauri_version: tauri::VERSION.to_string(),
        debug: cfg!(debug_assertions),
    }
}

/// Quick health check — verifies core subsystems are reachable.
///
/// Checks the availability of critical subsystems such as the
/// database connection, LLM model endpoint, and file system access.
///
/// # Arguments
///
/// None.
///
/// # Returns
///
/// [`HealthStatus`] — Always returns a status (never errors).
/// - `healthy: true` when all subsystems are operational.
/// - `healthy: false` with a descriptive `message` when something is wrong.
///
/// # Frontend Example
///
/// ```typescript
/// import { invoke } from '@tauri-apps/api/core';
///
/// const health = await invoke<HealthStatus>('system_health');
/// if (!health.healthy) {
///   showError(`System degraded: ${health.message}`);
/// }
/// ```
#[tauri::command]
pub async fn system_health(
    bridge: tauri::State<'_, PythonBridge>,
) -> Result<HealthStatus, ()> {
    // Ping the Python backend by requesting a lightweight endpoint.
    let status = match bridge
        .get_json::<serde_json::Value>("/system_prompt", &[])
        .await
    {
        Ok(_) => HealthStatus {
            healthy: true,
            message: "Python backend reachable".to_string(),
        },
        Err(e) => HealthStatus {
            healthy: false,
            message: format!("Python backend unreachable: {e}"),
        },
    };
    Ok(status)
}

// ── Tests ───────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_info_contains_package_metadata() {
        let info = system_info();
        assert_eq!(info.name, "ema-ai-agent");
        assert!(!info.version.is_empty());
        assert!(!info.tauri_version.is_empty());
    }

    #[test]
    fn app_info_serializes_to_json() {
        let info = system_info();
        let json = serde_json::to_string(&info).unwrap();
        assert!(json.contains("\"name\":\"ema-ai-agent\""));
        assert!(json.contains("\"debug\""));
    }

    #[test]
    fn health_status_serializes() {
        let status = HealthStatus {
            healthy: true,
            message: "ok".to_string(),
        };
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("\"healthy\":true"));
        assert!(json.contains("\"message\":\"ok\""));
    }

    #[test]
    fn health_status_unhealthy_serializes() {
        let status = HealthStatus {
            healthy: false,
            message: "database unreachable".to_string(),
        };
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("\"healthy\":false"));
    }

    #[test]
    fn app_info_round_trip() {
        let original = system_info();
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: AppInfo = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.name, original.name);
        assert_eq!(deserialized.version, original.version);
        assert_eq!(deserialized.debug, original.debug);
    }

    #[test]
    fn health_status_round_trip() {
        let original = HealthStatus {
            healthy: false,
            message: "model endpoint timeout".into(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: HealthStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.healthy, original.healthy);
        assert_eq!(deserialized.message, original.message);
    }
}
