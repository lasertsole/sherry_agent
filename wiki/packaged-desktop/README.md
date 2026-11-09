# Packaged Desktop Startup Reliability

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> How the unified Tauri installer survives its first Windows run: shipped config templates are found inside the PyInstaller bundle, the QQ SDK ships with the backend instead of being pip-installed at runtime, the tray close action has its window capability, and client-input mistakes return 400 instead of 500.

## First-run configuration

The shell passes `--sherry-home <app-data>`; `config/_home.resolve_code_root()` prefers `sys._MEIPASS` (the onedir `_internal` directory) for SHIPPED templates, while `ROOT_DIR` keeps its existing role as the project/skills root. `sherry.jsonc`, `.models`, `.network` and `.env` seed from that template root on first import. A missing template now logs a warning instead of silently skipping the seed, so a broken packaged layout is diagnosable from the log.

Single-file templates need one more packaging rule: PyInstaller `--add-data`'s DEST is a DIRECTORY. Passing `.models.example` as the destination creates a directory of that name, so the first-run seeder sees a directory and skips it — the model/app config tabs then render empty. The build adds every file entry to the bundle root (`.`), `check_seed_files` rejects a frozen tree whose four seed templates are not regular files, and `tests/packaging/test_build_backend.py` pins both.


## Frozen channels and QQ

A frozen backend has no virtual environment and cannot install packages into its own interpreter. `channels/deps.install_requirements()` and `channels/registry._ensure_deps()` short-circuit when `sys.frozen` is true. The QQ SDK (`botpy`, package `qq-botpy`) is a project dependency and is collected by `packaging/build_backend.py`, so the plugin imports it normally. If a build omits it, the channel logs one INFO and disables itself instead of retrying `uv pip install` on every start.

## Tray close behavior

`CloseToTrayDialog` calls `getCurrentWindow().hide()` through Tauri IPC. `core:default` only grants read-only window introspection, so `client/src-tauri/capabilities/default.json` explicitly grants `core:window:allow-hide`. Without it the dialog acknowledges the close but the window stays visible; the capability is compiled into the Rust shell, so a rebuilt installer is required.

## HTTP client-input errors

The packaged route probe found five handlers that let a deliberate client error escape to the generic 500 funnel. Missing or unsafe media parameters, missing or non-integer history page fields, a missing pending-interrupt session, a missing or invalid `DELETE /sessions` body, and a non-integer memory entry id now answer the shared `bad_request`/`not_found` responses (400/404) at the HTTP boundary.

## Verification

Focused suites cover the frozen template resolver and all four seed files, the frozen channel install short-circuits, the bundled QQ SDK collection, the tray capability, and the five 400 responses. A Windows installer must still be rebuilt and tested on a fresh profile for the end-to-end first-run flow.

Source: `config/_home.py`, `config/path.py`, `config/sherry_settings.py`, `config/models_settings.py`, `config/network_settings.py`, `channels/deps.py`, `channels/registry.py`, `plugins/channels/qq/core.py`, `packaging/build_backend.py`, `server/trigger/http/`, `client/src-tauri/capabilities/default.json`.
