# Packaging the desktop app

Two commands turn the checkout into an installer. Run them **on the target OS**
— PyInstaller cannot cross-compile, so Linux/Windows/macOS each build their own.

```bash
uv run --with pyinstaller python packaging/build_backend.py   # ~15 min, ~7 GiB
cd client && pnpm tauri build --bundles deb                   # deb; also appimage/msi/dmg
```

## What each piece does

- **`backend_entry.py`** — the frozen program's entry point. It imports
  `server.__main__.main` **statically** and calls it. That import is
  load-bearing: a `runpy.run_module("server")` is invisible to PyInstaller's
  analysis, which is how the first build shipped without the `server` package at
  all. `main()` chdirs next to the executable when frozen.
- **`build_backend.py`** — the PyInstaller **onedir** build (a onefile build
  would unpack gigabytes on every launch). It collects our packages with
  `--collect-submodules` (their `__getattr__` uses `importlib.import_module`,
  invisible to the freezer), ships the data the backend reads from the code
  tree (`skills/`, `workspace/template/`, `plugins/`, `sherry.jsonc`,
  `.env.example`, `context_engine/`), and afterwards copies this document into
  the artifact directory as its README. Most of the size is PyTorch.
- **`client/src-tauri/tauri.conf.json`** — `frontendDist` points at the
  `nuxt generate` output (`.output/public`; the `nuxt build` output needs a
  Nitro server and cannot be served from disk), built by
  `beforeBuildCommand` = `pnpm build:desktop`, and `bundle.resources` ships the
  backend tree as the `sherry-backend` resource (installed to
  `/usr/lib/<product>/sherry-backend/` on Linux).
- **`client/src-tauri/src/services/python_process.rs`** — picks the backend:
  a packaged app always spawns `<resource_dir>/sherry-backend/sherry-backend`
  with `SHERRY_HOME` = the OS app-data directory (everything writable — sessions,
  memory, logs, learned skills, `.env` — lives there, never in the read-only
  install tree), while a checkout run can opt into `python -m server` through
  `EMA_AUTO_START_BACKEND`.

## Do not symlink the resource directory

Never point `client/src-tauri/target/release/sherry-backend` (or an installed
`/usr/lib/<product>/sherry-backend`) back at the source directory to make an
unbundled run find the backend. At build time `tauri-build` copies
`bundle.resources` into `target/release/` with `std::fs::copy`, which truncates
the destination **before** reading the source — through such a symlink the
destination resolves to the source file itself, so the copy reads the file it
just emptied and the whole tree is rewritten as zero bytes. Observed once: one
`tauri build` left 7,634 zero-byte files and the bundler shipped an empty
payload. `tauri-build`'s same-file guard does not catch it (it compares a
canonicalized source against a joined, non-canonicalized target string).
`build_backend.py` removes such a symlink before building; if an unbundled run
needs the backend, run the installed layout instead of symlinking the source.
