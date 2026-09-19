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

## Phase 1 contract corrections

### Keep provider-specific weather parameters in the adapter

- **Decision:** The domain `WeatherRequest` contains only coordinates and aware
  datetimes. The Open-Meteo adapter will always add `timezone=auto` and convert
  its response into the domain result.
- **Alternative:** Expose the Open-Meteo timezone query parameter in the domain
  port.
- **Why:** Domain callers should express what forecast they need, not how a
  particular vendor encodes it. This also prevents provider details leaking
  into fixtures or future adapters.

### Separate mobility, transport, and pace

- **Decision:** Store physical accessibility needs, selected transport mode,
  and pace as independent fields.
- **Alternative:** Infer transport from mobility or pace.
- **Why:** "Limited walking," "I don't have a car," and "take it slowly" are
  independent constraints and may all apply to one trip.

### Use a typed planning boundary

- **Decision:** Planner and validator receive a `PlanningContext` containing
  candidate summaries, weather flags, a travel matrix, pace factors, and the
  server-injected current time.
- **Alternative:** Pass an untyped gathered-data dictionary.
- **Why:** Both components must consume the same verified inputs, and missing or
  misspelled data should fail at the boundary rather than inside search logic.

### Normalize recoverable LLM output

- **Decision:** LLM-facing schemas deduplicate repeated values, discard a stray
  clarification question when its flag is false, and promote exposure words
  from tags into `required_exposure`. Contradictory or semantically incomplete
  output is still rejected.
- **Alternative:** Reject every schema inconsistency immediately.
- **Why:** Harmless model variance should not create avoidable failures. The
  orchestration policy is: on invalid LLM output, retry once with the validation
  error, then use a deterministic fallback.

### Return model usage with every LLM result

- **Decision:** Text and structured calls return an output plus model id, token
  counts, and latency.
- **Alternative:** Return the output directly and collect metrics only inside a
  provider adapter.
- **Why:** Usage belongs to the observable call result and must remain available
  to orchestration logs and evaluations without provider-specific access.

### Keep the precomputed travel matrix synchronous

- **Decision:** `TravelTimeProvider.matrix` is synchronous and has no travel
  date parameter.
- **Alternative:** Model it as a live, date-dependent network call.
- **Why:** The scoped implementation reads a precomputed walking matrix. A
  synchronous contract accurately represents its behavior and stays testable.

### Reuse the OSM opening-hours grammar

- **Decision:** Depend on `opening-hours-py` and `tzdata` for OSM expression
  parsing, holiday support, timezone rules, and DST behavior.
- **Alternative:** Implement a partial parser for the catalog's expressions.
- **Why:** Opening-hours edge cases are safety- and feasibility-relevant. A
  maintained grammar implementation is smaller and less error-prone than a
  take-home-specific parser; our code will still own conservative visit-fit and
  last-entry decisions.

## Phase 2a — catalog draft

### Keep runtime values separate from verification metadata

- **Decision:** POI fields stay simple YAML values, with a parallel
  `needs_verification` map keyed by field path.
- **Alternative:** Wrap every value in `{value, needs_verification}`.
- **Why:** The planner and catalog loader should not need to unwrap every field,
  while reviewers still get explicit field-level provenance status.

### Represent unknown hours as empty, never as closed

- **Decision:** Managed venues without assignment-supplied hours use an empty
  `opening_hours` list and a verification flag. Requirement-designated public
  spaces use explicit year-round `24/7` periods with night-safety notes.
- **Alternative:** Insert plausible schedules or interpret missing hours as
  closed.
- **Why:** Both alternatives create false operational claims. Unknown data must
  prevent a definitive plan until verified or obtained live.

### Preserve the archaeological-museum conflict

- **Decision:** Keep the assignment-supplied Discover Greece seasonal split as
  the preferred demo value and store the April–October third-party statement in
  `conflicts`.
- **Alternative:** Silently replace it with the newest page found during research.
- **Why:** The requested case demonstrates source precedence and conservative
  conflict disclosure. Production behavior should prompt confirmation when the
  stakes are real.

### Treat coordinates as reviewable routing inputs

- **Decision:** Store four-decimal representative points and flag all of them.
- **Alternative:** Present them as verified entrances or omit coordinates.
- **Why:** Approximate points allow schema and matrix work to proceed, but large
  areas and monuments need actual pedestrian entrances before route generation.

### Own the holiday calendar explicitly

- **Decision:** Store 2026 national observances and the October 26 Thessaloniki
  holiday in `data/holidays.yaml`, all marked for verification.
- **Alternative:** Delegate public holidays to `opening-hours-py` immediately.
- **Why:** Venue closure behavior differs by holiday and library databases can
  be incomplete. Phase 2b will test the explicit file before any integration.

## Phase 2a correction — official museum data

### Replace stale tourism hours with the venue's official schedule

- **Decision:** The Archaeological Museum's official hours page, verified on
  2026-09-20, replaces both Discover Greece and travel-blog schedules. Both
  stale values remain in `conflicts` as third-party incident evidence.
- **Alternative:** Preserve the earlier assignment-provided schedule because it
  had already been encoded and tested.
- **Why:** Source precedence must change behavior, not merely documentation. The
  stale `08:00-20:00` value would send a visitor at 18:00 after the official
  17:00 close—the exact failure this architecture is intended to prevent.

### Model exceptional dates above seasonal schedules

- **Decision:** Add `temporary_closures` and `date_overrides`; per-POI closure
  rules refer to stable holiday ids. Resolution order is temporary closure,
  date override, per-POI holiday closure, then seasonal expression.
- **Alternative:** Encode every exception inside one large OSM expression or
  apply a global holiday-closed rule.
- **Why:** Explicit layers are easier to inspect and test. They also allow the
  Archaeological Museum to remain open with free admission on October 28 while
  still closing on its own listed holidays.

### Keep partial verification visible

- **Decision:** Store the Museum of Byzantine Culture's verified summer and
  winter periods but keep `opening_hours` flagged because April 1–May 7 is
  unknown; last entry remains separately flagged despite an official-directory
  source.
- **Alternative:** Fill the spring gap from a different current page or mark the
  entire schedule verified.
- **Why:** A partially sourced schedule must not become a complete operational
  claim through inference.
