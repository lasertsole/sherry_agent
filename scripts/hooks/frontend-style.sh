#!/usr/bin/env bash
# Frontend style gate for pre-commit: runs the client/ pnpm toolchain.
#
# Pre-commit passes repo-root-relative paths, but the toolchain must run with
# client/ as CWD (flat-config + pnpm exec resolution) — strip the prefix.
set -euo pipefail

tool="$1"
shift

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

args=()
for p in "$@"; do
  if [[ $p == client/* ]]; then
    args+=("${p#client/}")
  else
    args+=("$p")
  fi
done

exec pnpm exec "$tool" "${args[@]}"
