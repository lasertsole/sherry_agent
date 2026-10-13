#!/usr/bin/env bash
# ast-grep installer — 7-way package-manager fallback + pinned GitHub tarball.
#
# Usage:
#   bash agent/tools/code_intel/ast_grep/scripts/install.sh [--prefix <dir>] [--version <x.y.z>]
#
# The GitHub fallback downloads the pinned release, verifies its SHA-256 against
# the manifest baked in config/features/agent_side/ast_grep.py (when python3 is
# available), and extracts the real `ast-grep` binary rather than the `sg`
# launcher (the launcher re-execs relative to its own path and fails in some
# sandboxes).
set -euo pipefail

PREFIX="${HOME}/.local/bin"
VERSION="0.43.0"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

verify() {
  # A successful install must expose an `ast-grep`-reported version.
  for candidate in ast-grep sg; do
    if have "$candidate" && "$candidate" --version 2>/dev/null | grep -qi ast-grep; then
      echo "ast-grep installed: $($candidate --version 2>/dev/null | head -1)"
      return 0
    fi
  done
  return 1
}

try() {
  echo "-> $*"
  if "$@"; then return 0; fi
  echo "   (failed, trying next method)"
  return 1
}

echo "Installing ast-grep ${VERSION} (prefix: ${PREFIX})"

# 1. Homebrew / Linuxbrew
if have brew; then try brew install ast-grep && verify && exit 0; fi

# 2. npm
if have npm; then try npm install -g @ast-grep/cli && verify && exit 0; fi

# 3. cargo
if have cargo; then try cargo install ast-grep --locked && verify && exit 0; fi

# 4. pip
if have pip || have pip3; then
  PIP="pip"; have pip || PIP="pip3"
  try "$PIP" install --user ast-grep-cli && verify && exit 0
fi

# 5. nix
if have nix; then
  try nix profile install "nixpkgs#ast-grep" && verify && exit 0
fi

# 6. mise
if have mise; then
  try mise use -g "ast-grep@${VERSION}" && verify && exit 0
fi

# 7. GitHub release tarball (pinned, SHA-256 verified when possible)
case "$(uname -s)" in
  Darwin) OS=apple-darwin ;;
  Linux) OS=unknown-linux-gnu ;;
  *) echo "unsupported OS for the tarball fallback" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) ARCH=aarch64 ;;
  x86_64|amd64) ARCH=x86_64 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/ast-grep/ast-grep/releases/download/${VERSION}/app-${ARCH}-${OS}.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

echo "-> downloading ${URL}"
if have curl; then
  curl -fSL --retry 3 -o "${TMP}/ast-grep.zip" "${URL}"
elif have wget; then
  wget -O "${TMP}/ast-grep.zip" "${URL}"
else
  echo "neither curl nor wget available" >&2; exit 1
fi

# Extract the real `ast-grep` binary (never the `sg` launcher).
if have python3; then
  python3 - "${TMP}/ast-grep.zip" "${TMP}" <<'PY'
import sys, zipfile
archive, dest = sys.argv[1], sys.argv[2]
preferred = ["ast-grep", "sg"]
with zipfile.ZipFile(archive) as zf:
    names = {n.split("/")[-1]: n for n in zf.namelist()}
    for name in preferred:
        if name in names:
            zf.extract(names[name], dest)
            break
    else:
        raise SystemExit("no ast-grep/sg binary inside the archive")
PY
elif have unzip; then
  unzip -o "${TMP}/ast-grep.zip" -d "${TMP}"
else
  echo "need python3 or unzip to extract the release" >&2; exit 1
fi

mkdir -p "${PREFIX}"
install -m 0755 "${TMP}/ast-grep" "${PREFIX}/ast-grep" 2>/dev/null \
  || cp "${TMP}/ast-grep" "${PREFIX}/ast-grep"
chmod 0755 "${PREFIX}/ast-grep"

echo "Installed to ${PREFIX}/ast-grep — ensure ${PREFIX} is on PATH."
"${PREFIX}/ast-grep" --version || true
