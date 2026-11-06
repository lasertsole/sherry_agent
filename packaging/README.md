# Packaging the desktop app

A release is **one unified installer**: the static SPA, the Tauri Rust shell and
the CPU Python backend tree are packed into the same artifact
(`bundle.resources = ["sherry-backend/"]`). There is no separate backend archive
and no first-run backend download. PyInstaller cannot cross-compile, so each OS
builds its own backend tree immediately before the Tauri bundle.

The optional Linux CUDA acceleration pack remains a separate opt-in asset, built
by `packaging/build_cuda_pack.py`.

`.github/workflows/desktop-build.yml` builds the unified installer on all three
platforms (artifacts for a push to `main`, a GitHub Release for a `v*` tag).

```bash
# Linux: avoid dragging the CUDA family into the CPU installer
uv sync --frozen
uv pip freeze | grep -Ei '^(nvidia-|cuda-|triton)' | cut -d= -f1 | xargs -r uv pip uninstall
uv pip uninstall torch torchvision
uv pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0" "torchvision==0.29.0"

# build the backend tree, then the installer
uv pip install pyinstaller
uv run --no-sync python packaging/build_backend.py
cd client && pnpm tauri build
```

`--no-sync` is load-bearing: a plain `uv run` would re-sync the venv from the
lock and silently put CUDA PyTorch back on Linux.

## What each piece does

- **`backend_entry.py`** — the frozen program's entry point. It imports
  `server.__main__.main` **statically** and calls it. That import is
  load-bearing: a `runpy.run_module("server")` is invisible to PyInstaller's
  analysis, which is how the first build shipped without the `server` package at
  all. `main()` chdirs next to the executable when frozen.
- **`build_backend.py`** — the PyInstaller **onedir** build (a onefile build
  would unpack gigabytes on every launch). It collects our packages with
  `--collect-submodules` (their `__getattr__` uses `importlib.import_module`,
  invisible to the freezer), ships the data the backend reads from the code tree
  (`skills/`, `workspace/template/`, `plugins/`, `sherry.jsonc`, `.env`,
  `.models.example`, `.network.example`, `context_engine/`), and writes the
  **`VERSION` marker** plus a copy of this document into the tree. Most of the
  size is PyTorch. A first run seeds the app-data home's `.env` from the tracked
  one and its `.models` / `.network` from the two templates; those template
  lookups use `sys._MEIPASS` (`_internal`) via `config/_home.resolve_code_root`,
  because a frozen module's `__file__` is not a reliable anchor. The QQ channel
  SDK (`botpy`) is collected with `COLLECT_ALL`, so a frozen process never tries
  to `pip install` it at runtime.
- **Code embedder (optional)** — the code-intel semantic layer's Jina v2 Base
  Code ONNX backend is collected through `onnxruntime`; its 612 MB ONNX weights
  and tokenizer files are downloaded at runtime into
  `models/embed_model/code/`, never shipped in the backend tree. BGE remains the
  text embedder (memory / multimodal RAG).
- **`VERSION`** — the installer version this tree belongs to, read from
  `client/src-tauri/tauri.conf.json` (the same field the shell reports as
  `package_info().version`). The shell refuses a bundled tree whose marker
  differs, so it must never come from a git tag or a branch name. Pinned by
  `tests/packaging/test_build_backend.py`.
- **`client/src-tauri/tauri.conf.json`** — `frontendDist` points at the
  `nuxt generate` output (`.output/public`; the `nuxt build` output needs a
  Nitro server and cannot be served from disk), built by `beforeBuildCommand` =
  `pnpm build:desktop`, and `bundle.targets` is `"all"` so each platform's own
  bundler runs (deb/rpm/AppImage, nsis/msi, dmg/app). `bundle.resources` packs
  `sherry-backend/` into the installer.
- **`client/src-tauri/src/services/backend_bundle.rs`** — locates the bundled
  executable under the Tauri resource directory and checks its `VERSION` against
  `package_info().version`. A missing or stale tree is a broken install; there
  is no runtime download fallback.
- **`client/src-tauri/src/services/python_process.rs`** — spawns the bundled
  backend with `SHERRY_HOME` = the OS app-data directory (everything writable —
  sessions, memory, logs, learned skills, `.env` — lives there, never in the
  read-only install tree). A **dev** build (`pnpm tauri dev`, or a plain
  `cargo build`) neither packs nor starts the frozen backend and runs the
  checkout's `python -m server` when `EMA_AUTO_START_BACKEND` asks for it.

## Release hygiene (no tests, evals, docs or wiki)

Both halves are audited before they are uploaded. `packaging/check_release.py`
fails the build when a shipped tree contains:

- a `tests/`, `evals/`, `wiki/` or `__tests__/` directory (any depth
  outside PyInstaller's `_internal` dependency payload);
- a `test_*.py` file, or a colocated `*.test.*` / `*.spec.*` frontend test file;
- a Markdown file in the SPA (`client/.output/public`), whose payload is code
  and assets only.

`build_backend.py` runs the backend half itself after freezing; the desktop
workflow runs both halves explicitly before bundling/upload:

```bash
uv run --no-sync python packaging/check_release.py backend client/src-tauri/sherry-backend
python3 packaging/check_release.py frontend client/.output/public
```

Two exemptions are deliberate. Third-party tests under `_internal` are not this
repository's suite, and the multimodal_rag skill ships two documented manual
import utilities named `rag_import*_test.py`; neither is a test suite. The
shipped `packaging/README.md` and `skills/**/SKILL.md` are functional assets,
not project documentation. The Docker image applies the same boundary through
`.dockerignore` (which also excludes `evals/`).

## CPU-only PyTorch (Linux)

PyPI's Linux `torch` wheel declares the whole CUDA family (`cuda-*`, `nvidia-*`,
`triton` — ~5 GiB, measured) as dependencies, and none of it helps the CPU
installer. The workflow drops those packages and re-installs `torch` /
`torchvision` from PyTorch's CPU index before freezing; that index proxies PyPI
for every other name, so only the two torch packages change wheels. macOS and
Windows wheels are already CPU-only.

## Do not symlink the backend tree

Never point `client/src-tauri/target/release/sherry-backend` (or an installed
`/usr/lib/<product>/sherry-backend`) back at the source directory. `tauri-build`
copies `bundle.resources` with `std::fs::copy`, which truncates the destination
**before** reading the source — through such a symlink the copy read back the
file it had just emptied, and one `tauri build` left 7,634 zero-byte files in
place of the tree. `build_backend.py` removes a leftover symlink of that name
before building the real tree.
