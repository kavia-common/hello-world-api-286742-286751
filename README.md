# hello-world-api-286742-286751

A simple Flask REST API exposing:
- Health endpoint: GET /
- Hello endpoint: GET /hello

API documentation is available via Swagger UI at /docs.

Getting started:
- Dependencies are listed in flask_backend/requirements.txt
- The service runs using flask_backend/run.py
- The server binds to 0.0.0.0 and uses PORT env var if set, default 3001.

Example:
- Health: GET https://<host>:3001/
- Hello: GET https://<host>:3001/hello
- Docs:  GET https://<host>:3001/docs