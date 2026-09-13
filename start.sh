#!/bin/bash


CURRENT_DIR=$(pwd)

# Step 1: Start backend server
cd "${CURRENT_DIR}"

# Auto-detect the venv layout: POSIX venvs use bin/, Windows venvs (e.g. run
# from Git Bash / MSYS) use Scripts/.
if [ -x "./.venv/bin/python" ]; then
    VENV_BIN="./.venv/bin"
elif [ -x "./.venv/Scripts/python.exe" ]; then
    VENV_BIN="./.venv/Scripts"
else
    echo "Failed to find .venv interpreter (looked in bin/ and Scripts/), exiting"
    exit 1
fi

source "$VENV_BIN/activate" || { echo "Failed to activate virtual env, exiting"; exit 1; }

# Step 2: Start client frontend (Nuxt dev server, http://localhost:3000) in the
# background so it never blocks the backend startup below.
# Note: nuxt.config.ts sets server.strictPort — if port 3000 is already taken the
# client exits immediately, check logs/client_dev.log.
CLIENT_PID=""
kill_tree() {
    local pid=$1 child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child"
    done
    kill "$pid" 2>/dev/null
}
cleanup() {
    if [ -n "$CLIENT_PID" ]; then
        kill_tree "$CLIENT_PID"
    fi
}
trap cleanup EXIT INT TERM

if command -v pnpm >/dev/null 2>&1; then
    mkdir -p "${CURRENT_DIR}/logs"
    if [ ! -d "${CURRENT_DIR}/client/node_modules" ]; then
        CLIENT_CMD="pnpm install && pnpm dev"
        echo "client/node_modules missing, running 'pnpm install' + dev server in background..."
    else
        CLIENT_CMD="pnpm dev"
    fi
    cd "${CURRENT_DIR}/client"
    nohup bash -c "$CLIENT_CMD" > "${CURRENT_DIR}/logs/client_dev.log" 2>&1 &
    CLIENT_PID=$!
    cd "${CURRENT_DIR}"
    echo "Client frontend starting in background (pid ${CLIENT_PID}), log: logs/client_dev.log, URL: http://localhost:3000"
else
    echo "pnpm not found, skipping client frontend startup (backend only)"
fi

# Step 3: Start backend server in the foreground
"$VENV_BIN/python" -m server --fast --disable-openapi || { echo "Backend server exited with error"; exit 1; }
