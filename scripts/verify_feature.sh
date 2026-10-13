#!/usr/bin/env bash
#
# Feature verification gate — functional suites + regression gates in one command.
#
# Runs, in order:
#   client · typecheck (vue-tsc)          type-level safety for the SPA
#   client · unit tests                   pure logic + stores (vitest)
#   client · integration tests            mounted SFCs (vitest)
#   client · circular deps (dpdm)         import-cycle gate
#   repo   · pytest gate                  tests/run_tests_split.py (unit + integration + regression)
#   repo   · docs parity                  four-language README structure/metrics
#   repo   · doc links                    Markdown dead links + anchors
#
# Usage:
#   scripts/verify_feature.sh              # everything (a few minutes)
#   scripts/verify_feature.sh --quick      # client-only (skips the ~3 min pytest gate)
#   scripts/verify_feature.sh --backend-only
#
# Exits non-zero when any step fails; prints a per-step summary with timings.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT_DIR="$ROOT_DIR/client"

RUN_CLIENT=1
RUN_BACKEND=1
for arg in "$@"; do
  case "$arg" in
    --quick) RUN_BACKEND=0 ;;
    --backend-only) RUN_CLIENT=0 ;;
    -h | --help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --quick / --backend-only / --help)" >&2
      exit 2
      ;;
  esac
done

NAMES=()
RESULTS=()
TIMINGS=()

# run_step <name> <workdir> <command...>
run_step() {
  local name="$1"
  shift
  local dir="$1"
  shift
  echo ""
  echo "──────────────────────────────────────────────────────────────"
  echo "▶ ${name}"
  echo "──────────────────────────────────────────────────────────────"
  local start=$SECONDS
  if (cd "$dir" && "$@"); then
    RESULTS+=("PASS")
  else
    RESULTS+=("FAIL")
  fi
  NAMES+=("${name}")
  TIMINGS+=("$((SECONDS - start))s")
}

if [ "$RUN_CLIENT" = 1 ]; then
  run_step "client · typecheck (vue-tsc)" "$CLIENT_DIR" pnpm run typecheck
  run_step "client · unit tests" "$CLIENT_DIR" pnpm test:unit
  run_step "client · integration tests" "$CLIENT_DIR" pnpm test:integration
  run_step "client · circular deps (dpdm)" "$CLIENT_DIR" pnpm run dpdm
fi

if [ "$RUN_BACKEND" = 1 ]; then
  run_step "repo · pytest gate (unit + integration + regression)" \
    "$ROOT_DIR" uv run --no-sync python tests/run_tests_split.py
  run_step "repo · docs parity (four languages)" \
    "$ROOT_DIR" uv run --no-sync python scripts/check_docs_parity.py
  run_step "repo · doc links" \
    "$ROOT_DIR" uv run --no-sync python scripts/check_doc_links.py
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "VERIFICATION SUMMARY"
echo "══════════════════════════════════════════════════════════════"
failed=0
for i in "${!NAMES[@]}"; do
  printf '  %-4s %-58s %s\n' "${RESULTS[$i]}" "${NAMES[$i]}" "${TIMINGS[$i]}"
  [ "${RESULTS[$i]}" = "PASS" ] || failed=1
done

if [ "$failed" = 0 ]; then
  echo "VERDICT: PASS — all steps green"
else
  echo "VERDICT: FAIL — see the failing step(s) above"
fi
exit "$failed"
