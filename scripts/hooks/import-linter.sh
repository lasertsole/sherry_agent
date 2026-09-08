#!/usr/bin/env bash
# Layer dependency contract check for pre-commit.
#
# Runs import-linter on the server package to enforce architectural
# layering rules. Uses lint-imports CLI from import-linter.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

exec uv run --no-sync lint-imports
