mod utils;

use utils::logger::setup_logger;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Initialize tracing before Tauri starts (early init pattern).
    // This makes structured logging available during app bootstrap.
    let tracing_builder = setup_logger();

    tauri::Builder::default()
        .plugin(tracing_builder.build())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
