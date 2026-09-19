# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 2a is complete: the Phase 1 foundation now has a draft 22-POI catalog,
field-level verification metadata, a human verification queue, and an explicit
2026 holiday file. Opening-hours execution, the walking matrix, planning,
retrieval, orchestration, and the UI remain intentionally deferred to their
corresponding checkpoints.

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
