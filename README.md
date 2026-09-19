# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 2 is complete: the Phase 1 foundation now has a validated 22-POI catalog,
field-level verification metadata, an explicit 2026 holiday file, deterministic
opening-hours checks, and a committed walking matrix. Planning, retrieval,
orchestration, and the UI remain deferred to their corresponding phases.

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

Regenerate `data/walking_matrix.json` with `make matrix`. If `ORS_API_KEY` is
configured, the command makes one OpenRouteService foot-walking matrix request;
otherwise it uses the documented approximate fallback.
