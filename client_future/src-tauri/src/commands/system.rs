//! System IPC commands — health check, version info, app metadata.
//!
//! These commands have no direct Python backend equivalent; they
//! are new Tauri-native utilities for the desktop client.

use serde::{Deserialize, Serialize};

// ── Response types ──────────────────────────────────────────

/// Application metadata returned by the system.
#[derive(Debug, Clone, Serialize, Deserialize)]
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

/// Health-check response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthStatus {
    pub healthy: bool,
    /// Human-readable status description.
    pub message: String,
}

// ── Commands ────────────────────────────────────────────────

/// Return application metadata.
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
#[tauri::command]
pub async fn system_health() -> HealthStatus {
    // TODO: check database connectivity, model availability, etc.
    HealthStatus {
        healthy: true,
        message: "all systems operational".to_string(),
    }
}

// ── Tests (written FIRST to define the contract) ────────────

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
}
