// Prevents an additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! The app binary: Tauri's CLI bundles THIS executable, so it must exist.
//!
//! All wiring lives in the library (`lib.rs` → `run()`), which is also what the
//! mobile entry point reuses; keeping the binary a thin shim is the Tauri 2
//! template shape.

fn main() {
    app_lib::run();
}
