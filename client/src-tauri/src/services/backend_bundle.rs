//! Locate and validate the Python backend bundled inside a packaged installer.
//!
//! A release no longer downloads a backend archive: `pnpm tauri build` packs the
//! PyInstaller tree through `bundle.resources`, and the shell starts that tree
//! directly. The `VERSION` marker still guards the copy — a stale resource tree
//! is never served.

use std::path::{Path, PathBuf};

use crate::services::download::versions_match;

/// The backend tree's directory name inside the Tauri resource directory.
pub const BACKEND_DIR: &str = "sherry-backend";
/// The version marker inside that tree (`packaging/build_backend.py` writes it).
pub const VERSION_FILE: &str = "VERSION";

/// The backend executable's file name on this platform.
pub fn backend_exe_name() -> &'static str {
    if cfg!(windows) {
        "sherry-backend.exe"
    } else {
        "sherry-backend"
    }
}

/// The bundled backend tree under a resource directory.
pub fn bundled_backend_dir(resource_dir: &Path) -> PathBuf {
    resource_dir.join(BACKEND_DIR)
}

/// The bundled backend executable, regardless of whether it exists.
pub fn expected_backend_path(resource_dir: &Path) -> PathBuf {
    bundled_backend_dir(resource_dir).join(backend_exe_name())
}

/// The bundled executable when it is present AND its `VERSION` matches this
/// installer's version.
pub fn resolve_bundled_backend(resource_dir: &Path, app_version: &str) -> Option<PathBuf> {
    let tree = bundled_backend_dir(resource_dir);
    let exe = tree.join(backend_exe_name());
    let stored = std::fs::read_to_string(tree.join(VERSION_FILE)).ok()?;
    (exe.is_file() && versions_match(&stored, app_version)).then_some(exe)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "sherry-backend-bundle-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_matching_tree_resolves_to_its_executable() {
        let root = temp_dir("match");
        let tree = bundled_backend_dir(&root);
        std::fs::create_dir_all(&tree).unwrap();
        std::fs::write(tree.join(backend_exe_name()), b"#!/bin/sh\n").unwrap();
        std::fs::write(tree.join(VERSION_FILE), "v0.1.6\n").unwrap();

        assert_eq!(
            resolve_bundled_backend(&root, "0.1.6"),
            Some(expected_backend_path(&root))
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn an_absent_or_stale_tree_is_refused() {
        let root = temp_dir("stale");
        assert_eq!(resolve_bundled_backend(&root, "0.1.6"), None);

        let tree = bundled_backend_dir(&root);
        std::fs::create_dir_all(&tree).unwrap();
        std::fs::write(tree.join(backend_exe_name()), b"#!/bin/sh\n").unwrap();
        std::fs::write(tree.join(VERSION_FILE), "0.1.5\n").unwrap();

        assert_eq!(resolve_bundled_backend(&root, "0.1.6"), None);
        let _ = std::fs::remove_dir_all(&root);
    }
}
