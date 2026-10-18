# Packaging the desktop app

A release is **two artifacts**, built separately:

* the **installer** — the SPA + the Rust shell only (~30 MiB), from `pnpm tauri build`;
* the **backend tree** — a PyInstaller onedir with CPU-only PyTorch, from
  `packaging/build_backend.py`, published as the per-platform release asset the
  shell downloads on the app's first launch.

Splitting them is what makes the installer small and the release possible at all:
GitHub caps a release asset at 2 GiB, and a bundled backend is an order of
magnitude past that.

`.github/workflows/desktop-build.yml` builds both on all three platforms
(artifacts for a push to `main`, a GitHub Release for a `v*` tag). Run either half
by hand only when you must — both are per-OS, PyInstaller cannot cross-compile.

```bash
# backend tree, then the release asset's shape (tree contents at the archive root)
uv run --with pyinstaller python packaging/build_backend.py
cd client/src-tauri/sherry-backend && tar czf ../sherry-backend-linux-x86_64.tar.gz .

# installer (beforeBuildCommand runs `nuxt generate` for you)
cd client && pnpm tauri build
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
  invisible to the freezer), ships the data the backend reads from the code tree
  (`skills/`, `workspace/template/`, `plugins/`, `sherry.jsonc`, `.env`,
  `.models.example`, `.network.example`, `context_engine/`), and writes the
  **`VERSION` marker** plus a copy of this document into the artifact. Most of
  the size is PyTorch. A first run seeds the app-data home's `.env` from the
  tracked one and its `.models` / `.network` from the two templates.
- **`VERSION`** — the installer version this tree belongs to, read from
  `client/src-tauri/tauri.conf.json` (the same field the shell reports as
  `package_info().version`). The shell re-downloads whenever the marker differs,
  so it must never come from a git tag or a branch name. Pinned by
  `tests/packaging/test_build_backend.py`.
- **`client/src-tauri/tauri.conf.json`** — `frontendDist` points at the
  `nuxt generate` output (`.output/public`; the `nuxt build` output needs a
  Nitro server and cannot be served from disk), built by `beforeBuildCommand` =
  `pnpm build:desktop`, and `bundle.targets` is `"all"` so each platform's own
  bundler runs (deb/rpm/AppImage, nsis/msi, dmg/app). There is **no**
  `bundle.resources` entry: the backend is not part of the installer.
- **`client/src-tauri/src/services/backend_downloader.rs`** — the first-run
  flow: `<app_home>/sherry-backend/sherry-backend` present *and* its `VERSION`
  matching → spawn it; otherwise download
  `sherry-backend-<os>-<arch>.tar.gz|.zip` from the repository's latest release,
  unpack it into a staging directory, swap it in, and only then spawn it.
  Progress reaches the UI as Tauri events (`backend-download-started` /
  `-progress` / `-complete` / `-error`), with `backend_download_status` as the
  pull half for a front end that mounted late.
- **`client/src-tauri/src/services/python_process.rs`** — spawns the backend
  with `SHERRY_HOME` = the OS app-data directory (everything writable —
  sessions, memory, logs, learned skills, `.env` — lives there, never in the
  read-only install tree). A **release** build always starts the downloaded
  backend; a **dev** build (`pnpm tauri dev`, or a plain `cargo build`) neither
  ships nor downloads one and runs the checkout's `python -m server` when
  `EMA_AUTO_START_BACKEND` asks for it.

## Release hygiene (no tests, docs or wiki)

Both halves are audited before they are uploaded. `packaging/check_release.py`
fails the build when a shipped tree contains:

- a `tests/`, `docs/`, `wiki/` or `__tests__/` directory (any depth outside
  PyInstaller's `_internal` dependency payload);
- a `test_*.py` file, or a colocated `*.test.*` / `*.spec.*` frontend test file;
- a Markdown file in the SPA (`client/.output/public`), whose payload is code
  and assets only.

`build_backend.py` runs the backend half itself after freezing; the desktop
workflow runs both halves explicitly before compression/upload:

```bash
uv run --no-sync python packaging/check_release.py backend client/src-tauri/sherry-backend
python3 packaging/check_release.py frontend client/.output/public
```

Third-party tests under `_internal` are exempt (a wheel may ship its own tests),
and the multimodal_rag skill's documented manual `rag_import*_test.py` utilities
are not test suites. The shipped `packaging/README.md` and `skills/**/SKILL.md`
are functional assets, not project documentation. The Docker image applies the
same boundary through `.dockerignore`.

## The archive layout contract

The archive holds the tree's **contents at its root** (`cd sherry-backend &&
tar czf … .`), so unpacking it into `<app_home>/sherry-backend/` reproduces the
tree; a `sherry-backend/`-wrapped archive is accepted too. Unpacking uses the
system `tar` on every platform — GNU tar auto-detects the gzip stream, and
Windows 10+'s always-present `tar.exe` (bsdtar) also reads zip — so no unpacking
crates are needed.

## CPU-only PyTorch (Linux)

PyPI's Linux `torch` wheel declares the whole CUDA family (`cuda-*`, `nvidia-*`,
`triton` — ~5 GiB, measured) as dependencies, and none of it helps a desktop app
that runs CPU inference. The workflow drops those packages and re-installs
`torch` / `torchvision` from PyTorch's CPU index before freezing; that index
proxies PyPI for every other name, so only the two torch packages change wheels.
macOS and Windows wheels are already CPU-only.

A hand-built Linux backend from a plain `uv sync` therefore carries ~5 GiB of
CUDA libraries, and its archive will not fit GitHub's 2 GiB asset limit — use
the workflow, or repeat the swap:

```bash
uv pip freeze | grep -Ei '^(nvidia-|cuda-|triton)' | cut -d= -f1 | xargs -r uv pip uninstall
uv pip uninstall torch torchvision
uv pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0" "torchvision==0.29.0"
```

## Exercising the first-run flow without a release

`EMA_BACKEND_URL_BASE` overrides the release base URL, so the whole download →
unpack → spawn path runs against a local server:

```bash
# a stand-in archive: the executable + a matching VERSION, at the archive root
mkdir -p /tmp/fake/sherry-backend && cd /tmp/fake/sherry-backend
printf '#!/bin/sh\necho spawned >> /tmp/fake/started.log\nexec sleep 600\n' > sherry-backend
chmod 755 sherry-backend && echo "0.1.0" > VERSION
tar czf ../sherry-backend-linux-x86_64.tar.gz .
cd /tmp/fake && python3 -m http.server 8000 &

# a release build pointed at it (a dev build skips the whole flow by design)
cd client && pnpm tauri build --no-bundle
rm -rf ~/.local/share/com.ema-ai.agent/sherry-backend
EMA_BACKEND_URL_BASE=http://127.0.0.1:8000 ./src-tauri/target/release/ema-ai-agent
# → the window shows the download card, and /tmp/fake/started.log gains its line
```

## Do not symlink the backend tree

Never point `client/src-tauri/target/release/sherry-backend` (or an installed
`/usr/lib/<product>/sherry-backend`) back at the source directory. While the
backend was a bundle resource, `tauri-build` copied it with `std::fs::copy`,
which truncates the destination **before** reading the source — through such a
symlink the copy read back the file it had just emptied, and one `tauri build`
left 7,634 zero-byte files in place of the tree. The backend is no longer a
bundle resource, so that particular copy no longer happens;
`build_backend.py` still removes a leftover symlink of that name, and an
unpacked run should use the installed layout (or `EMA_BACKEND_URL_BASE`) instead
of a symlink.
