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
"$VENV_BIN/python" -m server --fast --disable-openapi || { echo "Backend server exited with error"; exit 1; }
