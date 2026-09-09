#!/usr/bin/env bash
set -euo pipefail

echo "ERROR: SKILL.md files must live under skills/ — stray files:" >&2
printf '  %s\n' "$@" >&2
echo "Hint: move them under skills/ (e.g. skills/<skill-name>/SKILL.md) or remove them." >&2
exit 1