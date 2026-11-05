//! Window lifecycle IPC commands.
//!
//! The Rust `CloseRequested` handler prevents the default close and asks the
//! frontend; these commands are how it answers:
//! - [`close_ack`] — "I am handling this request" (stops the fallback hide);
//! - [`quit_app`] — "quit for real" (the dialog's / preference's exit path).

use std::sync::atomic::Ordering;

/// Acknowledge the newest `close-requested` emission.
///
/// Called as soon as the frontend's listener runs — BEFORE its dialog appears —
/// so the 1.5 s fallback cannot hide a window that is about to show a prompt.
#[tauri::command]
pub fn close_ack() {
    crate::CLOSE_ACKED.store(crate::CLOSE_TICKET.load(Ordering::SeqCst), Ordering::SeqCst);
}

/// Quit the application for real.
///
/// Sets `EXPLICIT_QUIT` so the close interception lets the window go; the
/// `RunEvent::Exit` hook stops the Python backend.
#[tauri::command]
pub fn quit_app(app: tauri::AppHandle) {
    crate::EXPLICIT_QUIT.store(true, Ordering::SeqCst);
    app.exit(0);
}
