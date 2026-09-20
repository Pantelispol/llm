# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 4a is complete: the foundation and operational catalog now include
deterministic opening-hours and weather checks, an independent itinerary
validator, and an exhaustive small-request feasibility checker with numeric
breakdowns and minimal fixes. The Phase 4b planner, retrieval, orchestration,
and UI remain deferred to their corresponding checkpoints.

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
