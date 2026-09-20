# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 3 is complete: the foundation and operational catalog now include
deterministic opening-hours checks, a walking matrix, live Open-Meteo weather,
frozen weather scenarios, and policy-ready hourly safety flags. Planning,
retrieval, orchestration, and the UI remain deferred to their corresponding
phases.

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

Set `WEATHER_FIXTURE` to `clear_day`, `rain_after_16`, `heatwave_39`, or
`storm_evening` for a disclosed frozen scenario. Leaving it empty selects live
Open-Meteo data. Live smoke tests are excluded by default; run them explicitly
with `RUN_LIVE_WEATHER_TESTS=1 pytest -m live`.
