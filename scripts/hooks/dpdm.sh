#!/usr/bin/env bash
# Circular dependency detection gate for pre-commit.
#
# Entry is a glob over all TS modules, NOT app/app.vue: Nuxt auto-imports
# everything, so app.vue has no explicit imports and dpdm would traverse a
# vacuously empty graph. The glob covers the real .ts import graph; cycles
# routed exclusively through .vue SFC script blocks are out of dpdm's reach.
# --exit-code circular:1 exits non-zero when cycles are found (dpdm >=4
# dropped the old --warning exit semantics).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

exec pnpm exec dpdm 'app/**/*.{ts,mjs}' --circular --exit-code circular:1 --no-progress
