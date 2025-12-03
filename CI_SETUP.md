# CI and Local Lint Setup

The CI linter script invokes `flake8` from within the container workspace:
hello-world-api-286742-286751/.init/.linter.sh

To avoid "flake8: command not found", ensure flake8 is installed in this workspace venv:

1) Create venv in the container workspace:
   cd hello-world-api-286742-286751
   python3 -m venv .venv
   . .venv/bin/activate

2) Install dev requirements:
   pip install --upgrade pip
   pip install -r flask_backend/dev-requirements.txt

3) Run linter:
   . .venv/bin/activate
   flake8 flask_backend
