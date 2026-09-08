#!/usr/bin/env bash
# Frontend auto-fix gate for pre-commit: prettier --write + eslint --fix.
#
# If either tool modifies a file, pre-commit detects the modification and fails
# the hook. Re-stage with `git add` and retry — on the second run the files
# are already fixed, so this hook passes and the check hooks (eslint, vue-tsc)
# run next.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

# Collect staged frontend files (strip client/ prefix)
files=()
for p in "$@"; do
  if [[ $p == client/* ]]; then
    files+=("${p#client/}")
  fi
done

if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

# Step 1: prettier --write (covers ts/vue/css/scss/json that eslint cannot)
pnpm exec prettier --write "${files[@]}"

# Step 2: eslint --fix (lint + fix; don't fail on unfixable issues —
# the eslint check hook catches those)
pnpm exec eslint --fix "${files[@]}" || true
