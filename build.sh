#!/usr/bin/env bash
# One-click build script for EMA AI Agent.
#
# Usage:
#   ./build.sh all          # Build everything (backend + frontend + desktop)
#   ./build.sh backend      # Docker image only
#   ./build.sh frontend     # Nuxt static build only
#   ./build.sh desktop      # Tauri desktop installer only
#   ./build.sh help         # Show this message
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colors ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'
info()  { echo -e "${GREEN}[build]${NC} $1"; }
warn()  { echo -e "${YELLOW}[build]${NC} $1"; }
fail()  { echo -e "${RED}[build]${NC} $1" >&2; exit 1; }

# ── Targets ───────────────────────────────────────────────────────────

build_backend() {
  info "Building Docker image (backend)..."
  cd "$ROOT_DIR"

  [ -f .env ] || warn ".env not found — container will need --env-file at runtime"

  docker build -t sherry-agent:latest .
  info "Docker image: sherry-agent:latest"
  info "Run: docker run -p 8080:8080 --env-file .env sherry-agent:latest"
}

build_frontend() {
  info "Building Nuxt static frontend..."
  cd "$ROOT_DIR/client"

  if [ ! -d node_modules ]; then
    info "node_modules missing, running pnpm install..."
    pnpm install
  fi

  pnpm build
  info "Frontend output: client/dist/"
  info "Preview: cd client && pnpm preview"
}

build_desktop() {
  info "Building Tauri desktop installer..."
  cd "$ROOT_DIR/client"

  if [ ! -d node_modules ]; then
    info "node_modules missing, running pnpm install..."
    pnpm install
  fi

  command -v cargo >/dev/null 2>&1 || fail "Rust toolchain not found — install rustup first"

  pnpm tauri build
  info "Desktop installer: client/src-tauri/target/release/bundle/"
}

build_all() {
  build_backend
  echo ""
  build_frontend
  echo ""
  build_desktop
  info "All targets built successfully."
}

# ── CLI ───────────────────────────────────────────────────────────────

show_help() {
  cat <<EOF
EMA AI Agent — one-click build script.

Usage: ./build.sh <target> [options]

Targets:
  all        Build everything (backend Docker + frontend + desktop)
  backend    Build Docker image only (sherry-agent:latest)
  frontend   Build Nuxt static frontend only (dist/)
  desktop    Build Tauri desktop installer only (.msi/.dmg/.deb)
  help       Show this help message

Examples:
  ./build.sh backend
  ./build.sh all
EOF
}

case "${1:-help}" in
  all)      build_all ;;
  backend)  build_backend ;;
  frontend) build_frontend ;;
  desktop)  build_desktop ;;
  help|*)   show_help ;;
esac
