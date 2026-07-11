#!/bin/bash


CURRENT_DIR=$(pwd)

# Step 1: Start backend server
cd "${CURRENT_DIR}"
source ./.venv/Scripts/activate || { echo "Failed to activate virtual env, exiting"; exit 1; }
./.venv/Scripts/python -m server --fast --disable-openapi || { echo "Backend server exited with error"; exit 1; } &

# Step 2: Start Streamlit frontend
./.venv/Scripts/python -m streamlit run client/core.py || { echo "Frontend exited with error"; exit 1; }