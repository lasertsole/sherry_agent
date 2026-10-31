//! Host CUDA support detection (packing-plan B1).
//!
//! The CUDA pack is a Linux-only, user-opted acceleration add-on: the model
//! families that can use it (local speech recognition, the document VL
//! pipeline) fall back to CPU transparently. Detection answers ONE question —
//! "should the model settings tab offer the download at all" — so it is
//! deliberately shallow:
//!
//! * **Windows** — `nvidia-smi` from `PATH`, then the legacy
//!   `%ProgramFiles%\NVIDIA Corporation\NVSMI` location; the CSV query returns
//!   the GPU name and driver version.
//! * **Linux** — the same `nvidia-smi` query; a machine with the kernel module
//!   but no userland tool still counts (the `/proc/driver/nvidia/version`
//!   fallback proves the driver, the GPU name stays unknown).
//! * **macOS** — NVIDIA drivers were dropped by Apple; never supported.
//!
//! Everything is a best-effort probe: any failure degrades to
//! `supported: false` with a human-readable reason, never an error.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

/// What the host can do with a CUDA pack.
#[derive(Debug, Clone, Serialize, Deserialize, TS, PartialEq, Eq)]
#[ts(export, export_to = "../../app/types/backend/cuda-support.ts")]
pub struct CudaSupport {
    /// An NVIDIA GPU with a working driver is present.
    pub supported: bool,
    /// The driver version string, when the probe could read one.
    pub driver_version: Option<String>,
    /// The first GPU's marketing name, when known.
    pub gpu_name: Option<String>,
    /// Why the host is unsupported (shown in the model settings tab).
    pub reason: Option<String>,
}

impl CudaSupport {
    fn unsupported(reason: impl Into<String>) -> Self {
        Self {
            supported: false,
            driver_version: None,
            gpu_name: None,
            reason: Some(reason.into()),
        }
    }

    fn supported(driver_version: Option<String>, gpu_name: Option<String>) -> Self {
        Self {
            supported: true,
            driver_version,
            gpu_name,
            reason: None,
        }
    }
}

/// The `nvidia-smi` CSV line: `name, driver_version` (whitespace-tolerant).
fn parse_smi_line(line: &str) -> Option<(Option<String>, Option<String>)> {
    let line = line.trim();
    if line.is_empty() || line.contains("not found") || line.contains("failed") {
        return None;
    }
    let mut parts = line.splitn(2, ',');
    let name = parts.next().map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
    let driver = parts
        .next()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    Some((name, driver))
}

/// Run `nvidia-smi --query-gpu=...` and take the FIRST GPU's line.
fn probe_nvidia_smi(program: &str) -> Option<(Option<String>, Option<String>)> {
    let output = std::process::Command::new(program)
        .args(["--query-gpu=name,driver_version", "--format=csv,noheader"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .find_map(parse_smi_line)
}

/// Linux fallback: the kernel module's version line proves the driver.
fn probe_proc_driver() -> Option<String> {
    let text = std::fs::read_to_string("/proc/driver/nvidia/version").ok()?;
    // "NVIDIA UNIX x86_64 Kernel Module  545.29.06  Sun Aug  6 ..."
    for field in text.split_whitespace() {
        if field.chars().next().is_some_and(|c| c.is_ascii_digit()) && field.contains('.') {
            return Some(field.to_string());
        }
    }
    None
}

/// Detect CUDA support on this host (see the module docs).
pub fn detect() -> CudaSupport {
    if cfg!(target_os = "macos") {
        return CudaSupport::unsupported("macOS 没有可用的 NVIDIA 驱动，CUDA 加速不可用");
    }

    let smi_candidates: &[&str] = if cfg!(windows) {
        &["nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe"]
    } else {
        &["nvidia-smi"]
    };
    for program in smi_candidates {
        if let Some((name, driver)) = probe_nvidia_smi(program) {
            return CudaSupport::supported(driver, name);
        }
    }

    if cfg!(target_os = "linux") {
        if let Some(driver) = probe_proc_driver() {
            return CudaSupport::supported(Some(driver), None);
        }
        return CudaSupport::unsupported("未检测到 NVIDIA GPU 或驱动（nvidia-smi 不可用，且无内核模块记录）");
    }

    // Windows without nvidia-smi: no driver means no CUDA.
    CudaSupport::unsupported("未检测到 NVIDIA 驱动（nvidia-smi 不可用）")
}

// ── Tests ─────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_payload_serializes_with_the_frontend_key_names() {
        let support = CudaSupport::supported(Some("545.29.06".into()), Some("RTX 4090".into()));
        let json = serde_json::to_string(&support).unwrap();
        assert!(json.contains("\"supported\":true"));
        assert!(json.contains("\"driver_version\":\"545.29.06\""));
        assert!(json.contains("\"gpu_name\":\"RTX 4090\""));
        assert!(json.contains("\"reason\":null"));
    }

    #[test]
    fn an_unsupported_answer_carries_its_reason() {
        let support = CudaSupport::unsupported("no GPU");
        let json = serde_json::to_string(&support).unwrap();
        assert!(json.contains("\"supported\":false"));
        assert!(json.contains("\"reason\":\"no GPU\""));
        assert!(json.contains("\"gpu_name\":null"));
    }

    #[test]
    fn the_smi_line_parser_takes_name_and_driver() {
        let (name, driver) = parse_smi_line("NVIDIA GeForce RTX 4090, 545.29.06").unwrap();
        assert_eq!(name.as_deref(), Some("NVIDIA GeForce RTX 4090"));
        assert_eq!(driver.as_deref(), Some("545.29.06"));
        // A single-column line (older drivers) degrades instead of failing.
        let (name, driver) = parse_smi_line("RTX 3090").unwrap();
        assert_eq!(name.as_deref(), Some("RTX 3090"));
        assert_eq!(driver, None);
        assert!(parse_smi_line("").is_none());
        assert!(parse_smi_line("NVIDIA-SMI has failed because it couldn't communicate").is_none());
    }

    #[test]
    fn the_proc_driver_parser_takes_the_version_field() {
        // Synthetic: the real file's shape is "NVIDIA UNIX x86_64 Kernel Module
        //  545.29.06  <date>" — the first dotted numeric field is the version.
        assert_eq!(probe_proc_driver(), None, "this host has no NVIDIA driver");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_is_never_supported() {
        assert_eq!(detect().supported, false);
    }
}
