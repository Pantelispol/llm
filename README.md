# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
the LLM understands and narrates; deterministic code solves and verifies.

## Current status

Phase 5 is complete: the foundation and operational catalog now include
deterministic opening-hours and weather checks, an independent itinerary
validator, an exhaustive small-request feasibility checker, and a bounded beam
planner with minimal-perturbation repair. Tourism retrieval now combines BM25,
multilingual E5 embeddings, reciprocal-rank fusion, and a pgvector-backed
runtime store. Orchestration and the UI remain deferred to later checkpoints.

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
orchestration phase. Start PostgreSQL with pgvector using `make db-up`, then load
the RAG corpus with `make rag-ingest`. Set `RAG_STORE=memory` for a
dependency-free retrieval run.

Regenerate `data/walking_matrix.json` with `make matrix`. If `ORS_API_KEY` is
configured, the command makes one OpenRouteService foot-walking matrix request;
otherwise it uses the documented approximate fallback.

Set `WEATHER_FIXTURE` to `clear_day`, `rain_after_16`, `heatwave_39`, or
`storm_evening` for a disclosed frozen scenario. Leaving it empty selects live
Open-Meteo data. Live smoke tests are excluded by default; run them explicitly
with `RUN_LIVE_WEATHER_TESTS=1 pytest -m live`.

Run the deterministic planner demos without an API key:

```bash
python -m app.planning.demo --scenario five_hours_history
python -m app.planning.demo --scenario rain_after_16
```

Available scenarios are `five_hours_history`, `no_museum_followup`,
`with_child_followup`, `rain_after_16`, `heatwave`, `shrink_to_two_hours`, and
`replace_second_indoor`.
