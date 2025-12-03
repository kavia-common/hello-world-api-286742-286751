# Local Development Quickstart

These steps assume a repo-level virtual environment `.venv` is used (as in CI).

1) Create venv and install dependencies:
   python3 -m venv .venv
   . .venv/bin/activate
   pip install --upgrade pip
   pip install -r hello-world-api-286742-286751/flask_backend/requirements.txt

2) Run linter:
   . .venv/bin/activate
   flake8 hello-world-api-286742-286751/flask_backend

3) Start the backend:
   . .venv/bin/activate
   python hello-world-api-286742-286751/flask_backend/run.py

The server binds to 0.0.0.0 and uses PORT env var if set, default 3001.
