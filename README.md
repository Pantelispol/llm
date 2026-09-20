# Thessaloniki Tourist AI Assistant

A deliberately small, production-minded prototype built around one boundary:
**the LLM understands and narrates; deterministic code solves and verifies.**
The model never chooses a time, a route, an opening hour or a price. It turns a
message into a structured analysis, and it turns a validated result into prose
whose every place name, time and citation is a token that code substitutes and
then re-checks.

## Architecture

Blue nodes are LLM calls. Green nodes are deterministic code. Orange marks a
deterministic gate that can reject or replace an LLM output. There are exactly
two LLM calls per turn, and neither of them is on the path that decides what
the itinerary is.

```mermaid
flowchart TD
    U([User message + TripState]):::io

    subgraph TURN["ConversationPipeline.run_turn"]
        direction TB
        LANG["Language detection<br/>Greek / English"]:::det
        UND["UNDERSTAND<br/>LLM · effort=none · JSON schema<br/>intents, entities, constraint deltas"]:::llm
        UFB["Understand fallback<br/>keyword rules on provider failure"]:::gate
        TIME["Temporal resolver<br/>'tomorrow', durations, Athens time"]:::det
        ROUTE["ROUTE<br/>intent → tool list policy table<br/>+ source-boundary overrides"]:::det
        GATHER["GATHER<br/>catalog · retriever (BM25 + E5 + RRF)<br/>weather · opening hours · walking matrix"]:::det
        PLAN["PLAN<br/>beam search + minimal-perturbation repair"]:::det
        VAL["VALIDATE<br/>independent itinerary validator"]:::det
        COMMIT["COMMIT<br/>TripState + itinerary_version<br/>only if a validated plan exists"]:::det
        NAR["NARRATE<br/>LLM · effort=low · text<br/>writes prose using {{poi:…}} {{time:…}} tokens"]:::llm
        POST["POST-CHECK<br/>catalog names, evidence ids, times,<br/>proper names, injected instructions"]:::gate
        RETRY["One retry with the violations listed"]:::llm
        TPL["Deterministic template answer<br/>used when the retry also fails"]:::gate
        VFAIL["Fixed validation-failure message<br/>state unchanged, violations returned"]:::gate
        RENDER["Render tokens into final text"]:::det
    end

    OUT([Answer + TripState + citations + trace]):::io

    U --> LANG --> UND
    UND -- provider failure --> UFB --> TIME
    UND --> TIME --> ROUTE --> GATHER --> PLAN --> VAL
    ROUTE -. "route does not touch the plan" .-> COMMIT
    VAL -- invalid --> VFAIL --> OUT
    VAL -- valid --> COMMIT --> NAR
    NAR --> POST
    POST -- rejected --> RETRY --> POST
    POST -- rejected twice --> TPL --> OUT
    POST -- accepted --> RENDER --> OUT

    classDef llm fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px,color:#0f172a
    classDef det fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#0f172a
    classDef gate fill:#ffedd5,stroke:#c2410c,stroke-width:2px,color:#0f172a
    classDef io fill:#f1f5f9,stroke:#475569,color:#0f172a
```

| Colour | Meaning | Steps |
|---|---|---|
| Blue | LLM call | `UNDERSTAND`, `NARRATE`, narration retry |
| Green | Deterministic code | language, temporal resolution, route, gather, plan, validate, commit, render |
| Orange | Deterministic guard or fallback | understand fallback, post-check, template answer, validation-failure message |

Two properties follow from the diagram and are enforced by tests:

- **The router runs on the model's own analysis.** The deterministically
  resolved time window is folded in *after* routing, so a question that merely
  contains "tomorrow" is never mistaken for a plan edit
  (`tests/test_pipeline.py::test_question_mentioning_tomorrow_leaves_state_untouched`).
- **A turn that produces no validated proposal commits nothing.** `TripState`
  and `itinerary_version` are left untouched.

### Router policy table

The router (`app/orchestrator/router.py`) is a lookup, not a model. Tools run in
a canonical order regardless of the order intents were listed.

| Intent | Tools | Touches the plan |
|---|---|---|
| `tourism_qa` | catalog, retriever | no |
| `recommendation` | catalog, retriever | no |
| `create_plan` | catalog, weather, hours, travel, planner, validator | yes |
| `edit_plan` | catalog, weather, hours, travel, planner, validator | yes |
| `feasibility_check` | catalog, weather, hours, travel, feasibility | no (read-only) |
| `weather_question` | weather | no |
| `opening_hours_question` | catalog, hours | no |
| `price_question` | catalog | no |
| `safety` | catalog, weather | no |
| `unsupported_live_info` | none | no |
| `out_of_scope` | none | no |
| `small_talk` | none | no |

Overrides applied on top of the model's intents:

| Trigger in the user's text | Effect | Why |
|---|---|---|
| Opening-hours language (`open`, `closes`, `ωράριο`, …) | Replaces `tourism_qa` / `recommendation` with `opening_hours_question` | Hours come from the catalog and hours engine, never from retrieved prose |
| Price language (`price`, `ticket`, `εισιτήριο`, …) | Replaces `tourism_qa` / `recommendation` with `price_question` | Prices come from the operational catalog, never from retrieval |
| Plan language (`plan`, `itinerary`, `replace`, `remove`, …), any plan intent, any plan edit, or any constraint update from the model | Turn touches the plan | Plan tools run and the planner may commit a new version |

Retrieved tourism text is untrusted: it is quoted as evidence, flagged
`draft`, and checked against instruction-like patterns before it can reach the
answer.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
make test
```

The default `LLM_MODE=replay` needs no API key: every model call is answered
from recorded fixtures under `evals/fixtures/llm/`. `LLM_MODE=live` and
`LLM_MODE=record` require `LLM_API_KEY`.

### PostgreSQL / pgvector

Retrieval uses a pgvector store. Set `RAG_STORE=memory` to skip the database
entirely.

```bash
make db-up
make rag-ingest
```

If host port 5432 is already taken, start this project's database on 5433 and
point the connection URL at it:

```bash
POSTGRES_PORT=5433 make db-up
DATABASE_URL=postgresql://tourist:tourist@localhost:5433/tourist make rag-ingest
```

For repeated use put both `POSTGRES_PORT=5433` and the matching `DATABASE_URL`
in `.env`.

### Weather and walking times

Set `WEATHER_FIXTURE` to `clear_day`, `rain_after_16`, `heatwave_39` or
`storm_evening` for a disclosed frozen scenario. Leaving it empty selects live
Open-Meteo data. Live smoke tests are excluded by default:
`RUN_LIVE_WEATHER_TESTS=1 pytest -m live`.

`data/walking_matrix.json` is regenerated with `make matrix`. With
`ORS_API_KEY` set it makes one OpenRouteService foot-walking matrix request;
otherwise it uses a documented approximate fallback, which the answer discloses.

## Demos

### Three-turn CLI conversation (no API key)

The assignment's scenario: *five hours tomorrow* → *no museum* → *with my
10-year-old*. Each turn receives only the `TripState` returned by the previous
one, never the earlier transcript.

```bash
make chat-demo
```

which is:

```bash
LLM_MODE=replay WEATHER_FIXTURE=clear_day RAG_STORE=memory \
  python -m app.chat_cli --scenario assignment --now 2026-09-21T12:00:00+03:00
```

`--now` is injected so "tomorrow" is reproducible against the recorded
fixtures. It prints the answer, the state, the stage trace, tools called,
violations, disclosures and token usage for each turn.

### Deterministic planner (no API key, no model at all)

```bash
python -m app.planning.demo --scenario five_hours_history
python -m app.planning.demo --scenario rain_after_16
```

Scenarios: `five_hours_history`, `no_museum_followup`, `with_child_followup`,
`rain_after_16`, `heatwave`, `shrink_to_two_hours`, `replace_second_indoor`.

### HTTP API

```bash
make run                     # uvicorn app.main:app --reload
```

`GET /health`, and `POST /chat` with `{message, trip_state}`, which returns the
answer, the new `trip_state`, `tools_called`, `violations`, `citations`,
`disclosures` and fallback flags.

## Make targets

| Target | What it does |
|---|---|
| `make test` | `pytest` (offline; no API key, no network) |
| `make lint` | `ruff check .` |
| `make run` | Start the API with `uvicorn app.main:app --reload` |
| `make matrix` | Regenerate `data/walking_matrix.json` |
| `make rag-ingest` | Embed and load the tourism corpus into the configured store |
| `make rag-eval` | Retrieval gold-set report (BM25, in-memory) |
| `make eval` | Per-component replay reports: LLM, understand, router, narration |
| `make eval-full` | Phase 7 end-to-end eval from recorded fixtures, 2 repetitions |
| `make chat-demo` | The three-turn CLI conversation, replayed offline |
| `make db-up` / `make db-down` | Start / stop PostgreSQL with pgvector (`POSTGRES_PORT` selects the host port) |

## Phase 7 results

`make eval-full` replays 20 cases from recorded fixtures; nothing is scored by a
model. Model variance is sampled once, at record time, and replayed
byte-for-byte. Each case reports a pass rate over its own recorded
repetitions. The denominator is **2** for every case except
`three_turn_continuity`, which is a conversation case recorded once (**1**).
Nothing was rewritten, softened or removed to make a case pass.

| Case | Coverage | Passed / recorded | Failure |
|---|---|---|---|
| rag_factual_citation | factual RAG Q&A with citation | 2/2 | |
| personalised_recommendation | personalised recommendation | **0/2** | `used_fallback=True`; plan tools ran on a recommendation |
| plan_creation | plan creation | 2/2 | |
| no_museum_exclusion | no-museum exclusion | 2/2 | |
| three_turn_continuity | three-turn continuity | 1/1 | |
| weather_replan_rain | weather replan, rain after 16:00 | 2/2 | |
| heat_replan | heat | 2/2 | |
| positional_edit | positional edit | 2/2 | |
| feasibility_feasible | feasible feasibility check | 2/2 | |
| feasibility_infeasible | infeasible feasibility check | 2/2 | |
| museum_last_entry | Archaeological Museum last-entry edge case | 2/2 | |
| public_holiday_closed | public-holiday edge case | 2/2 | |
| unsupported_events | unsupported live info (events) | 2/2 | |
| safety_storm_seich_sou | safety-critical storm plus Seich Sou | 2/2 | |
| adversarial_invent_attraction | adversarial instruction | 2/2 | |
| greek_variant_plan | Greek-language variant, planning | 2/2 | |
| price_question | admission price quoted from the catalog | 2/2 | |
| weather_question | weather answered from the forecast | 2/2 | |
| out_of_scope | out-of-scope request declined | 2/2 | |
| greek_variant_qa | Greek-language variant, factual Q&A | 2/2 | |

**18 of 20 cases pass every recorded repetition.** Counted per run that is 35 of
39 recorded runs (19 cases × 2 + 1 conversation run); the four failed runs are the
two cases at 0/2. The metric denominators below are 41 because they count
turns, and the conversation case has three.

| Metric | Value | Definition |
|---|---|---|
| Tool-selection accuracy | 95.1% (39/41) | `tools_called` equals the case's expected list |
| Validator violation rate | 0.0% (0/19) | committed plans carrying an ERROR violation |
| Entity hallucination, first draft | 7.3% (3/41) | drafts the post-check rejected for an ungrounded name |
| Entity hallucination, delivered | 0.0% (0/41) | zero by construction: names are substituted by the renderer |
| Citation validity | 100% (43/43) | the post-check rejects unknown evidence ids |
| Post-check first-draft pass rate | 90.2% (37/41) | narrations accepted with no retry |
| Fallback rate | 4.9% (2/41) | turns that fell back to the deterministic template |

"Delivered hallucination 0%" is a property of the renderer, not a measured model
quality. The model-quality figure is the first-draft rate, 7.3%.

**What changed since the first Phase 7 run.** The first run passed 13 of 20 cases.
Seven failed for one shared reason: the resolved time window was folded into the
model's constraint updates before routing, so any turn containing "tomorrow"
committed an itinerary. Routing now uses the model's own analysis. Tool-selection
accuracy rose from 70.7% (29/41) to 95.1% (39/41) and cases passing every
recorded repetition from 13 to 18. The forecast summary is now cited evidence,
so weather turns can answer from it. This also changed every weather-path
narration prompt, so those fixtures were re-recorded.

Retrieval gold-set numbers (BM25 baseline, 22 cases) print at the end of
`make eval-full`.

## Known limitations

- **`personalised_recommendation` falls back to the deterministic template**
  in both repetitions, and it runs plan tools (weather, hours, travel) it should
  not. The understand model returns `add_interests` for this turn, and the router
  treats any constraint update as a plan touch. That matches the observed tool
  list, but I have not proven it is the cause of the fallback itself. It is the
  same class of defect as the "tomorrow" bug, one level down.
- **Phase 4d was never implemented.** Window utilization is computed by counting
  idle time as used, so the utilization term rewards filling a window with
  waiting or rest as much as with visits. Plans can look better packed than they
  are.
- **The content corpus is LLM-drafted.** Every file in `data/content/` carries
  `confidence: draft`. Nothing has been reviewed by a person against a primary
  source. The human review queue is `docs/data-verification.md`; the catalog's
  `needs_verification` map is the machine-readable version. Operational facts
  (hours, prices) are handled separately and some are marked verified, but the
  prose the retriever serves is not. Answers built on it carry a draft-content
  disclosure.
- **The gold sets were authored alongside the corpus they test.** The retrieval
  recall/MRR figures and the eval cases were written by the same process, at the
  same time, against the same documents. They show the system is internally
  consistent and regression-safe; they are not evidence of generalisation to
  questions nobody wrote in advance.
- **Small samples.** Two repetitions per case, one for the conversation case, and
  20 cases in total. A 2/2 is not a reliability estimate. Repetition 3 was never
  recorded. Nothing here measures live latency: recorded latencies are replayed.
- **The project went well over the planned 6–8 hour budget.** The cause was scope,
  not difficulty in any one place. The deterministic core (hours engine,
  independent validator, exhaustive feasibility checker, beam planner with
  repair, plan-quality pass) took four checkpoints before any model was
  involved; retrieval grew from a BM25 baseline into dense embeddings, rank
  fusion, calibrated abstention and a pgvector store; and a record/replay
  layer plus an end-to-end harness had to be built so the LLM parts could be
  tested offline and byte-reproducibly. Those were choices that favoured a
  verifiable system over a fast one. I have not kept a measured hour count, so
  this document does not state one.

Design decisions and their reasons are in `docs/decisions-log.md`; per-phase
specs are in `docs/specs/`.
