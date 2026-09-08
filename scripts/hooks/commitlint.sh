#!/usr/bin/env bash
# Commit message lint gate for pre-commit (commit-msg stage).
#
# Runs @commitlint/cli with @commitlint/config-angular to enforce Angular
# commit convention on the commit message.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

# Pre-commit passes the path to .git/COMMIT_EDITMSG as $1
exec pnpm exec commitlint --edit "$1"
