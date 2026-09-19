# Decisions log

This is a running interview-preparation log. Each entry records a decision,
the main alternative, and the reason for the choice.

## Phase 1 — foundation

### Keep orchestration framework-free

- **Decision:** Use plain Python protocols and Pydantic contracts between layers.
- **Alternative:** LangChain or another agent framework.
- **Why:** The assignment explicitly values explainability and deterministic
  policy routing. Small interfaces expose control flow and are easy to test.

### Store exact time windows as timezone-aware datetimes

- **Decision:** `TimeWindow` carries aware start and end datetimes.
- **Alternative:** Separate local date and `time` fields, or unzoned strings.
- **Why:** Exact instants survive DST boundaries and eliminate hidden dependence
  on process-local timezone. Relative date resolution remains deterministic
  server work and will always produce `Europe/Athens` values.

### Keep potentially invalid itineraries representable

- **Decision:** Domain models validate local shape (positive activity duration)
  but do not reject cross-activity overlap, opening-hour, or weather problems.
- **Alternative:** Put all feasibility rules in Pydantic validators.
- **Why:** The independent itinerary validator must inspect a candidate and
  return explicit `violations[]`; failing model construction would hide those
  actionable diagnostics.

### Represent edits as discriminated operations

- **Decision:** Use typed `replace`, `remove`, `add`, and `move` operations with
  one-based positions matching user language.
- **Alternative:** Ask the LLM to emit a complete replacement itinerary.
- **Why:** Explicit edits let deterministic repair preserve every unaffected
  activity and make positional requests auditable.

### Make constraint changes additive/removal-based

- **Decision:** `ConstraintUpdates` has explicit add/remove lists.
- **Alternative:** Replace every state list with the LLM output.
- **Why:** Deltas avoid accidentally erasing preferences from earlier turns and
  support deterministic state reduction.

### Define ports before adapters

- **Decision:** Weather, hours, travel, retrieval, planner, validator, and LLM
  boundaries are Python `Protocol`s with Pydantic request/result models.
- **Alternative:** Let implementations return dictionaries.
- **Why:** Typed boundaries make tool arguments schema-validated, keep adapters
  replaceable, and prevent raw external data leaking into planning code.

### Start with one pgvector PostgreSQL service

- **Decision:** Docker Compose contains PostgreSQL 17 with pgvector only.
- **Alternative:** Add Redis, a vector database, and service containers now.
- **Why:** Postgres can support metadata, full-text search, and vectors. Extra
  infrastructure does not demonstrate the core thesis within the time budget.

