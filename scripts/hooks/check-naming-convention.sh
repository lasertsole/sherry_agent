#!/usr/bin/env bash
# File naming convention guard for pre-commit.
#
# Backend (.py):     snake_case.py      -> ok
# Frontend (.ts):    kebab-case.ts      -> ok
# Frontend (.vue):   PascalCase.vue     -> ok
# Style (.scss/css): kebab-case         -> ok
#
# Exemptions:
#   - compound suffixes: *.d.ts, *.config.ts, *.test.ts, *.spec.ts, *.integration.test.ts
#   - Nuxt-required .vue names: app.vue, error.vue, default.vue (layouts), index.vue
#     (pages), dynamic routes ([param].vue)
#   - files listed in scripts/hooks/naming-baseline.txt (grandfathered legacy
#     names — the debt registry shrinks as files get renamed; new files must
#     follow the convention and can NOT be added to the baseline)
set -euo pipefail

BASELINE="$(dirname "${BASH_SOURCE[0]}")/naming-baseline.txt"

violations=()

for f in "$@"; do
  base=$(basename "$f")
  name="${base%.*}"
  ext="${base##*.}"

  if grep -Fxq "$f" "$BASELINE" 2>/dev/null; then
    continue
  fi

  case "$ext" in
    py)
      if ! [[ "$name" =~ ^[a-z_][a-z0-9_]*$ ]]; then
        violations+=("$f → .py must be snake_case")
      fi
      ;;
    ts|tsx|js|jsx|mjs|cjs)
      if [[ "$name" =~ \.(d|config|test|spec|integration\.test|e2e\.test)$ ]]; then
        continue
      fi
      if ! [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]]; then
        violations+=("$f → .$ext must be kebab-case")
      fi
      ;;
    vue)
      if [[ "$name" =~ ^(app|error|default|index)$ ]] || [[ "$base" == \[* ]]; then
        continue
      fi
      if ! [[ "$name" =~ ^[A-Z][a-zA-Z0-9]+$ ]]; then
        violations+=("$f → .vue must be PascalCase")
      fi
      ;;
    scss|css)
      if ! [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]]; then
        violations+=("$f → .$ext must be kebab-case")
      fi
      ;;
  esac
done

if [ ${#violations[@]} -gt 0 ]; then
  echo "ERROR: file naming convention violations:" >&2
  printf '  %s\n' "${violations[@]}" >&2
  echo "" >&2
  echo "Naming rules:" >&2
  echo "  .py   → snake_case   (e.g. user_service.py)" >&2
  echo "  .ts   → kebab-case   (e.g. use-theme.ts)" >&2
  echo "  .vue  → PascalCase   (e.g. ChatBox.vue)" >&2
  echo "  .scss → kebab-case   (e.g. main.scss)" >&2
  echo "  (legacy files are listed in scripts/hooks/naming-baseline.txt — rename" >&2
  echo "   them instead of adding new entries)" >&2
  exit 1
fi
