//! Tracing initialization for EMA AI Agent.
//!
//! Uses the "early initialization" pattern so that tracing is
//! available **before** the Tauri app starts — useful for
//! diagnosing bootstrap failures.
//!
//! Configured layers:
//! - Console (colored output)
//! - File (daily rotation, ANSI stripped)
//! - Per-module level filtering
//!
//! Log files are written to:
//! - Windows: `%TEMP%/ema-ai-agent/app.YYYY-MM-DD.log`
//! - macOS:   `~/Library/Logs/ema-ai-agent/app.YYYY-MM-DD.log`
//! - Linux:   `/tmp/ema-ai-agent/app.YYYY-MM-DD.log`

use tauri_plugin_tracing::{tracing_appender, tracing_subscriber, Builder, StripAnsiWriter};
use tracing::Level;
use tracing_subscriber::filter::Targets;
use tracing_subscriber::fmt;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::Registry;

/// Initialize tracing before the Tauri app starts.
///
/// Returns a minimal [`Builder`] that should be passed to
/// `.plugin(builder.build())` so that the plugin's IPC commands
/// (`trace`, `debug`, `info`, `warn`, `error`) remain available
/// from JavaScript.
///
/// # Example
///
/// ```rust,ignore
/// fn main() {
///     let builder = setup_logger();
///     tauri::Builder::default()
///         .plugin(builder.build())
///         .run(tauri::generate_context!())
///         .expect("error while running tauri application");
/// }
/// ```
pub fn setup_logger() -> Builder {
    // --- Log directory ---
    let log_dir = std::env::temp_dir().join("ema-ai-agent");
    let _ = std::fs::create_dir_all(&log_dir);

    // --- File appender (daily rotation, non-blocking) ---
    let file_appender = tracing_appender::rolling::daily(&log_dir, "app");
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
    // Keep the guard alive for the entire application lifetime.
    // Dropping it would flush and close the file writer.
    std::mem::forget(guard);

    // --- Per-module filter ---
    let targets = Targets::new()
        .with_default(Level::DEBUG)
        // Suppress noisy third-party crates
        .with_target("hyper", Level::WARN)
        .with_target("hyper_util", Level::WARN)
        .with_target("reqwest", Level::WARN)
        .with_target("h2", Level::WARN)
        .with_target("rustls", Level::WARN)
        .with_target("tokio_util", Level::WARN)
        .with_target("tower", Level::WARN)
        .with_target("want", Level::WARN)
        // Tauri internals
        .with_target("tauri", Level::INFO)
        .with_target("wry", Level::WARN);

    // --- Compose subscriber layers ---
    Registry::default()
        // Console layer (colored output)
        .with(fmt::layer().with_ansi(true))
        // File layer (ANSI codes stripped)
        .with(
            fmt::layer()
                .with_writer(StripAnsiWriter::new(non_blocking))
                .with_ansi(false),
        )
        .with(targets)
        .init();

    // The global subscriber is now set. Return a minimal builder so
    // the plugin can register IPC commands for JavaScript interop.
    Builder::new()
}
