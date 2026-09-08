#!/usr/bin/env bash
# Dead code detection for the frontend.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../client"
exec pnpm exec knip --no-progress
