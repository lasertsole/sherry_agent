#!/usr/bin/env bash
# Drive the actual packaged Tauri app through W3C WebDriver.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/client"
exec node e2e/packaged/run.mjs "$@"
