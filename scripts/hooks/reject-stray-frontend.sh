#!/usr/bin/env bash
set -euo pipefail

echo "ERROR: file types css/rs/js/ts/mjs/vue must live under client/ — stray files:" >&2
printf '  %s\n' "$@" >&2
echo "Hint: move them into client/ or remove them." >&2
exit 1
