#!/bin/bash
set -euo pipefail

# Move to backend folder for linting scope
cd /home/kavia/workspace/code-generation/hello-world-api-286742-286751/flask_backend

# Try to activate a virtual environment in this workspace; fall back to repo-level .venv
if [ -f "../.venv/bin/activate" ]; then
  # activate venv at container workspace root
  source ../.venv/bin/activate
elif [ -f "../../.venv/bin/activate" ]; then
  # activate venv at repo root
  source ../../.venv/bin/activate
fi

# If flake8 still not on PATH, try installing minimal dev deps locally
if ! command -v flake8 >/dev/null 2>&1; then
  echo "flake8 not found on PATH; attempting local installation..."
  python -m pip install --upgrade pip >/dev/null 2>&1 || true
  python -m pip install flake8==7.2.0 >/dev/null 2>&1 || true
fi

# Run flake8 against this backend
flake8 .
LINT_EXIT_CODE=$?
if [ $LINT_EXIT_CODE -ne 0 ]; then
  exit 1
fi

