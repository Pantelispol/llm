# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 1 is complete: repository skeleton, domain schemas, typed dependency
interfaces, configuration, Docker Compose, and schema tests. Planning, tools,
retrieval, orchestration, and the UI are intentionally deferred to their
corresponding phases.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
make test
make run
```

The API currently exposes only `GET /health`; product endpoints arrive with the
orchestration phase. PostgreSQL with pgvector can be started with `make db-up`.

