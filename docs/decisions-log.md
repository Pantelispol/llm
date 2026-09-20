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
  datetimes. The Open-Meteo adapter adds its required timezone parameter and
  converts the response into the domain result.
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

## Phase 2b — operational facts

### Resolve opening rules in explicit precedence order

- **Decision:** Evaluate temporary closures, exact-date overrides, per-POI
  holiday closures, then seasonal expressions.
- **Alternative:** Merge all rules into a single generated OSM expression.
- **Why:** Separate layers produce clear reasons, make incident cases directly
  testable, and prevent a general holiday calendar from closing venues such as
  the Archaeological Museum on October 28 when it is explicitly open.

### Use the parser for syntax, not policy

- **Decision:** `opening-hours-py` evaluates the selected day's OSM expression;
  our code selects the source rule and checks last entry and full visit fit.
- **Alternative:** Pass all catalog facts to the library and trust its holiday
  database and next-change behavior.
- **Why:** The application must own provenance, exception precedence, and
  conservative unknown handling. Those are product rules, not parsing tasks.

### Reject unknown operational periods conservatively

- **Decision:** A date with no matching seasonal period returns “opening hours
  unknown” with `needs_verification=true` and cannot be scheduled.
- **Alternative:** Extend the nearest known season across the gap.
- **Why:** Extending May or winter hours into the Byzantine Museum's unknown
  April 1–May 7 gap would invent a time-sensitive fact.

### Round fallback walking times up

- **Decision:** When ORS is unavailable, calculate haversine distance × 1.3 at
  4.5 km/h and round up to whole minutes; every non-diagonal leg is marked
  approximate.
- **Alternative:** Round to nearest or truncate.
- **Why:** Underestimating travel harms feasibility. Upward rounding is a small,
  explicit conservative bias that the planner can explain.

### Commit the fallback matrix when no ORS key is present

- **Decision:** The Phase 2b artifact was generated without an `ORS_API_KEY`, so
  its source is `haversine_fallback`. The same command switches to one ORS
  foot-walking matrix request when a key is configured.
- **Alternative:** Block the phase until an external key is supplied.
- **Why:** The assignment explicitly permits the approximation, and its metadata
  makes the loss of routing accuracy visible rather than hidden.

## Phase 3 — weather providers and flags

### Give the planner stable weather decisions, not vendor measurements

- **Decision:** Convert hourly Open-Meteo values into `rain_risk`, `storm`,
  `heat_risk`, `uv_high`, and `after_dark` before planning.
- **Alternative:** Pass raw temperatures, WMO codes, probabilities, and sunset
  strings into the planner or LLM.
- **Why:** Named booleans make safety and planning rules deterministic, small,
  and testable. Raw values remain in `WeatherResult` for explanations and logs,
  but cannot silently change planner policy.

### Parse live and fixture weather through one path

- **Decision:** Store the captured Open-Meteo response shape and derive frozen
  scenarios by changing its hourly arrays; both providers call the same parser.
- **Alternative:** Hand-author already-normalized fixture domain objects.
- **Why:** Shared parsing catches vendor-shape drift and prevents fixtures from
  passing while live responses fail. `is_fixture` and the scenario source remain
  explicit so narration can disclose simulated weather.

### Degrade weather failures into typed results

- **Decision:** Retry timeouts, transport errors, and 5xx responses once, then
  return `WeatherResult.unavailable_reason` instead of raising to orchestration.
- **Alternative:** Let HTTP or parsing exceptions abort the whole turn.
- **Why:** Weather is mandatory for planning, but an upstream outage should
  produce a safe limitation or conservative fallback rather than a server error.
  Structured failure reasons are also measurable in evaluation and operations.

### Keep weather thresholds explicit and configurable

- **Decision:** Default rain risk to probability >=50% or WMO rain codes, storm
  to codes 95/96/99, heat risk to feels-like >=35C (>=32C with children), and
  high UV to >=8.
- **Alternative:** Ask the LLM to interpret each forecast or bury thresholds in
  planner branches.
- **Why:** The defaults are conservative, inspectable product policy. The lower
  child heat threshold demonstrates party-aware safety, and inclusive boundary
  tests prevent ambiguous behavior at exactly 50%, 35C, 32C, and UV 8.

### Fix the city timezone inside the Open-Meteo adapter

- **Decision:** Send `timezone=Europe/Athens` and parse returned local timestamps
  as timezone-aware Athens datetimes.
- **Alternative:** Send `timezone=auto` or add a timezone field to the domain
  request.
- **Why:** This product serves one fixed city. An explicit adapter constant is
  predictable around DST while keeping provider query mechanics out of the
  domain port.

## Phase 4a — validator and feasibility checker

### Recompute feasibility independently from the proposed itinerary

- **Decision:** Treat activity starts as proposed slots, then recompute visit
  ends from catalog durations and pace factors, travel from the matrix, opening
  status from the hours engine, and hazards from hourly flags. Stored visit and
  travel claims are never used as evidence of feasibility.
- **Alternative:** Trust planner-populated duration and travel fields and only
  check that activities do not overlap.
- **Why:** A planner bug would otherwise validate itself. Independent
  recomputation catches shortened visits, missing buffers, stale hours, and
  incorrect walking claims with a separate implementation boundary.

### Make violation codes a stable contract

- **Decision:** Define error and warning codes as uppercase string enums while
  keeping concrete, human-readable messages separate.
- **Alternative:** Infer failure types from free-text validation messages.
- **Why:** Evals, logs, the narrator, and future repair logic need identifiers
  that do not change when wording changes. Typed codes also prevent spelling
  drift across tests and orchestration.

### Treat unavailable weather as unknown risk

- **Decision:** Emit `WEATHER_UNAVAILABLE`, continue planning ordinary city
  POIs, and exclude every `safety_tier=critical` POI from feasibility results.
- **Alternative:** Treat an empty weather response as clear conditions or fail
  every plan outright.
- **Why:** Missing data is not evidence of safety, especially for trails. The
  warning preserves useful low-risk plans while the critical-tier exclusion
  enforces the conservative boundary deterministically.

### Exhaustively check small feasibility requests

- **Decision:** Enumerate every order for up to six POIs, prefer weather-safe
  orders, then select the shortest elapsed schedule and break ties
  lexicographically by `poi_id`.
- **Alternative:** Use a greedy nearest-neighbor order or the future beam-search
  planner.
- **Why:** At six stops the maximum 720 permutations remain small. Exhaustive
  search gives an explainable ground truth for “can I do these places?” and is
  independent from Phase 4b's heuristic planner.

### Apply pace as explicit, non-compounding arithmetic

- **Decision:** Relaxed pace contributes a 1.25 factor; children contribute 1.3
  for walking and 1.15 for visits. Each duration uses the maximum applicable
  factor rather than multiplying factors, then rounds up. Transition buffers
  default to five configurable minutes in `PlanningContext`.
- **Alternative:** Encode “relaxed” or “with children” as qualitative planner
  hints, or multiply the factors and cap the result.
- **Why:** Explicit multipliers make feasibility reproducible and prevent the
  narrator or LLM from silently compressing travel and visit durations. Taking
  the maximum avoids double-counting two descriptions of slower movement.

### Keep validator expectations independent from shared helpers

- **Decision:** Production planner and validator may share small lookup and
  rounding helpers, but validator tests use hand-computed matrix, buffer, pace,
  and clock expectations rather than calling `rules.py` to derive assertions.
- **Alternative:** Build test expectations with the same helper functions used
  by the code under test.
- **Why:** Shared helpers reduce duplication but can create correlated bugs. A
  wrong pace or rounding rule must make the validator tests fail, not update
  both the implementation and its expected value in lockstep.

## Phase 4b — beam planner and repair

### Use bounded beam search instead of a general solver

- **Decision:** Rank at most ten candidates, retain thirty partial plans per
  depth, and break ties by `poi_id`.
- **Alternative:** Model the itinerary as an OR-Tools optimization problem.
- **Why:** The search space is deliberately small. Beam search keeps time-window,
  weather, meal, and break decisions visible in ordinary Python and stays well
  within the interactive latency budget without adding a solver dependency.

### Keep scoring components and weights visible

- **Decision:** Current defaults are interest 4, must-see 2, weather 4, child
  suitability 4, diversity 1, travel 0.02, repair perturbation 3, and window
  utilization 12. Interest is earned from an actual preference match rather
  than a baseline; rain is strongly penalized, while storm and high-heat
  exposure are hard constraints. The child, travel, and utilization defaults
  supersede the initial Phase 4b values after the Phase 4c quality review.
- **Alternative:** Collapse quality into one opaque score or ask the LLM to rank
  complete itineraries.
- **Why:** Per-activity score components make planner behavior debuggable in the
  interview and give the narrator structured reasons without letting prose
  override safety or feasibility.

### Repair locally before a full re-plan

- **Decision:** Apply explicit edits and new constraints to the existing visit
  order, then re-time that order. Reorder weather-exposed stops before replacing
  them; use a full beam search only when the local plan remains invalid or
  undesirable, with a penalty for removing or reordering retained POIs.
- **Alternative:** Discard the itinerary and generate a fresh plan after every
  follow-up.
- **Why:** Users expect unmentioned choices to remain stable. The local-first
  path makes continuity the default, while deterministic validation still gates
  every repaired result.

### Encode meal and child-break policy as planning rules

- **Decision:** Windows of at least four hours receive one 45-minute meal at an
  open approved meal area near the route: lunch at 13:00–15:30 or dinner at
  19:30–21:30. Tsinari is eligible only when the route is already in Ano Poli.
  Parties with children receive a 15-minute break after roughly 90 minutes,
  never within 45 minutes after a meal.
- **Alternative:** Let narration suggest informal stops after the itinerary is
  built.
- **Why:** Meals and rests consume real time. Representing them as activities
  makes the validator account for them and prevents the prose layer from adding
  infeasible schedule commitments.

## Verified-hours data refresh

### Treat manual listing checks as curated input, not a production integration

- **Decision:** Record Tony's dated manual checks with explicit confidence and
  source kind. Preserve unknown weekdays and source conflicts instead of
  converting missing days into closures.
- **Alternative:** Treat every omitted weekday as closed or scrape listing pages
  during a request.
- **Why:** Unknown is operationally different from closed, and page scraping is
  brittle. A production system would ingest Google Places and official
  `odysseus.culture.gr` data through supported APIs, retain source timestamps,
  enforce freshness thresholds, and flag stale or conflicting schedules for
  review.

## Phase 4c — plan quality

### Feasible is not good

- **Decision:** Add reusable quality assertions for window use, thematic value,
  meal timing, child walking, weather placement, and drop-reason honesty on top
  of the independent feasibility validator.
- **Alternative:** Treat zero validator errors as sufficient plan quality.
- **Why:** A short, empty, or poorly ordered itinerary can be perfectly valid.
  Feasibility remains the safety gate; quality assertions measure whether the
  valid plan is useful.

### Reward useful window utilization explicitly

- **Decision:** Add a bounded utilization component to beam ranking and final
  scoring, targeting at least 80% elapsed-window use when enough candidates are
  open. Reduce raw walking cost from 0.05 to 0.02 per minute so it cannot erase
  the value of a worthwhile visit.
- **Alternative:** Keep adding positive visit scores and assume deeper search
  will naturally fill the window.
- **Why:** Meals, opening waits, and different visit lengths make visit count an
  unreliable proxy for a complete itinerary. The explicit term states the
  product objective without weakening any feasibility constraint.

### Repair weather in least-disruptive order

- **Decision:** Try all visit reorderings for plans of at most six stops, moving
  exposed activities before the risk window. If that fails, the beam search may
  swap or add indoor stops, but POIs that were already indoor or outside the
  risk window are required and cannot be removed as weather casualties.
- **Alternative:** Apply a weather penalty and allow a full re-plan to remove any
  activity.
- **Why:** Reordering preserves user choices. Replacement and removal are
  progressively more disruptive, while rain-exposed slots remain hard-invalid.

### Make every drop reason auditable

- **Decision:** Emit `CLOSED` only when no full typical visit fits an open
  interval, and `NOT_ENOUGH_TIME` only when a conservative occupied-time lower
  bound exceeds the window. All other eligible omissions use `LOWER_SCORE` with
  the non-negative static-score gap to the weakest selected visit; route costs
  and stable tie-breaking explain a zero-gap omission.
- **Alternative:** Report the first failed beam expansion as the final reason.
- **Why:** Expansion failures depend on one partial route and can falsely imply
  that a candidate never fit. Post-classifying the chosen plan produces stable,
  defensible explanations.

## ORS walking-matrix refresh

### Keep unit arithmetic independent from refreshable data artifacts

- **Decision:** Validator and feasibility unit tests load a small, fixed walking
  matrix from `tests/fixtures/`; only the catalog-coverage test reads the
  committed matrix and accepts either supported source when its `approximate`
  flag agrees with that source.
- **Alternative:** Update expected minutes and warning assertions each time the
  production matrix is regenerated.
- **Why:** ORS routing is the better runtime input, but route refreshes are data
  changes, not validator or feasibility behavior changes. Fixed legs keep
  hand-computed expectations meaningful and prevent external routing changes
  from masquerading as code regressions.

### Preserve compatible activities during a full repair

- **Decision:** Before a non-weather full replan, deterministically find the
  largest prior-order prefix/subsequence that still fits the updated
  constraints, require those compatible visits in the replacement plan, and
  apply twice the normal perturbation cost to removal versus reordering. Record
  an explicit constraint reason for prior activities that no longer fit.
- **Alternative:** Let the ordinary beam objective freely trade every prior
  activity against new candidates once local repair fails.
- **Why:** A full replan is an implementation fallback, not permission to erase
  unaffected user choices. Stronger removal cost keeps the search focused on
  continuity, while the compatibility pass avoids requiring an impossible stop
  such as a hilly POI after child walking constraints are introduced.

## Phase 5b — lexical tourism RAG baseline

### Keep BM25 explicit and dependency-free

- **Decision:** Implement the standard BM25 formula directly with `k1=1.5` and
  `b=0.75`, backed by literal, hand-computed toy-corpus scores.
- **Alternative:** Add `rank_bm25` or a retrieval framework.
- **Why:** The implementation is small enough to explain line by line, avoids a
  dependency for one formula, and makes IDF and length normalization directly
  testable.

### Index one alias chunk per POI

- **Decision:** Add one non-narrative alias chunk containing the English and
  Greek catalog names plus catalog and content-frontmatter aliases.
- **Alternative:** Repeat aliases inside every descriptive section or depend on
  multilingual embeddings for all name resolution.
- **Why:** A dedicated chunk gives lexical retrieval a deterministic path for
  local names without polluting prose or duplicating terms across every chunk.

### Normalize accents and Greek sigma without language-specific stemming

- **Decision:** Apply Unicode NFD, remove combining marks, map final sigma to
  ordinary sigma, lowercase, replace punctuation with spaces, and collapse
  whitespace.
- **Alternative:** Use separate Greek and English analyzers with stemming.
- **Why:** The small bilingual corpus mainly needs spelling-equivalent names to
  match. This transparent normalization handles those cases without opaque
  linguistic dependencies or aggressive changes to proper nouns.

### Retain an unused long-section splitting path

- **Decision:** Split sections above 220 words only at paragraph boundaries,
  even though the current short content sections never trigger the path.
- **Alternative:** Omit splitting until a document exceeds the limit.
- **Why:** Future content can grow without silently producing oversized prompt
  chunks. A synthetic test proves the otherwise dormant branch and guarantees
  that it never cuts a paragraph or sentence in the current input model.

## Phase 5c — dense retrieval, fusion, and abstention

### Keep the real encoder fixed and the test encoder explicit

- **Decision:** Use `intfloat/multilingual-e5-small` with its required query and
  passage prefixes and normalized 384-dimensional vectors. Use the explicitly
  named `HashEncoder` only when `RAG_DENSE=off`, and cache real passage vectors
  by model name plus corpus content hash.
- **Alternative:** Mock the E5 library throughout the application or download
  and encode the corpus on every process start.
- **Why:** Tests stay deterministic and offline without disguising the hash
  encoder as semantic retrieval. The cache makes normal startup cheap while a
  content edit or model change invalidates every stale vector.

### Fuse ranks rather than incomparable scores

- **Decision:** Retrieve the top 20 lexical and dense chunks independently and
  combine their ranks with reciprocal rank fusion at `k=60`.
- **Alternative:** Normalize and add BM25 and cosine scores directly.
- **Why:** BM25 and cosine live on different scales. RRF needs no fitted score
  weights, remains inspectable, and has a literal hand-computed unit test.

### Do not claim an abstention threshold when the frozen set does not separate

- **Decision:** Calibrate the required conjunctive dense/lexical rule against
  the frozen gold set, but leave thresholds unset when no pair retains every
  in-KB case and rejects every out-of-KB case. The 2026-09-20 E5 run found no
  valid pair: rejecting all out-of-KB cases requires a dense threshold above
  `0.855353` and a normalized-BM25 threshold above `0.575516`; the in-KB Roman
  monuments query scores `0.853061` dense and `0.456520` lexical, while the
  Greek Heptapyrgio query scores `0.837683` and `0.527697`. Both would be false
  abstentions at those rejection floors, so no chosen thresholds or margin are
  recorded.
- **Alternative:** Optimize aggregate accuracy or silently accept one false
  abstention to produce attractive precision/recall numbers.
- **Why:** The spec makes 100% in-KB retention and rejection of all four
  out-of-KB cases hard constraints. Reporting the overlap is more defensible
  than overfitting a tiny set or calling an imperfect cutoff calibrated.

### Treat every retrieved chunk as untrusted data

- **Decision:** Carry an `is_untrusted` provenance flag through retrieval and
  keep the poisoned instruction fixture outside the default content directory.
- **Alternative:** Sanitize instruction-like sentences during ingestion.
- **Why:** Retrieval must preserve source text for auditability while never
  granting it control authority. Phase 6 will delimit untrusted prompt blocks
  and post-check that operational claims come from the structured catalog.

## Phase 5d — pgvector store boundary

### Default the demo to pgvector and keep tests exact and dependency-free

- **Decision:** Put dense search behind one `VectorStore` protocol. Runtime
  selection defaults to `RAG_STORE=pgvector`; unit tests and CI select the
  exact numpy `InMemoryVectorStore`, which is also the evaluation reference.
- **Alternative:** Require PostgreSQL for every test, or keep numpy search as a
  separate code path outside the store contract.
- **Why:** The demo exercises the assignment's real vector-database component,
  while deterministic unit tests do not depend on Docker or approximate HNSW
  ranking. An unavailable pgvector runtime exits with instructions to run
  `make db-up` or use `RAG_STORE=memory`.

### Keep the vector schema model-specific

- **Decision:** Store normalized E5 vectors as `vector(384)`, upsert with raw
  parameterized SQL, and skip rows whose content hash has not changed.
- **Alternative:** Use an ORM or an unconstrained vector representation.
- **Why:** The fixed dimension lets pgvector validate data and build the HNSW
  operator class. Changing the embedding model or its dimensions requires a
  migration and a full re-embedding; it is not a transparent configuration
  change.

### Treat HNSW as a scaling path, not a small-corpus speed claim

- **Decision:** Keep the HNSW index, but report the planner's observed choice.
  On the 88-row Thessaloniki corpus, filtered `EXPLAIN ANALYZE` used a
  sequential scan plus top-N sort (`0.516 ms` execution), not HNSW. A
  rolled-back synthetic 384-dimensional test with a 50%-selective city filter
  still used a sequential scan at 2,000 rows and first selected HNSW at the
  next measured point, 2,250 rows (`0.052 ms` versus `0.346 ms` for the
  2,000-row sequential plan). This crossover is environment-, selectivity-,
  and query-dependent, not a universal threshold.
- **Alternative:** Force index scans or claim that creating an index means the
  planner uses it.
- **Why:** Exact brute-force scanning is cheaper at this corpus size. Letting
  PostgreSQL choose preserves the fast small-table plan while HNSW becomes
  useful as the corpus grows.

### Make filtered-HNSW underfill explicit

- **Decision:** Always filter by `city_id`, while documenting that pgvector's
  approximate index gathers `hnsw.ef_search` candidates before applying the
  `WHERE` filter. A selective city or POI filter can therefore return fewer
  than `top_k` rows even when enough matching rows exist.
- **Alternative:** Hide the edge case or over-fetch in application code without
  evidence that it is needed.
- **Why:** pgvector 0.8+ iterative index scans and, for a stable small set of
  destinations, partial indexes per city are the available mitigations. They
  are intentionally not implemented for the current 88-row corpus.

### Keep BM25 instead of relabeling PostgreSQL full-text ranking

- **Decision:** Retain the prototype's own BM25 for the lexical half of hybrid
  retrieval.
- **Alternative:** Replace it with PostgreSQL `tsvector` / `ts_rank` and call
  that BM25.
- **Why:** PostgreSQL full-text search is a useful lightweight lexical path
  without a second service, but `ts_rank` is not BM25: it does not provide the
  same IDF and document-length normalization.

### Record the same-gold-set measurement

- **Decision:** Run all 23 frozen gold queries through E5 against both stores.
  Memory and pgvector produced identical quality: BM25 recall@5/MRR
  `0.852/0.862`, dense `0.963/1.000`, and hybrid `0.944/0.917`; there was no
  retrieval delta. From comparable cached-corpus runs, mean latency in
  milliseconds was memory `0.063/286.375/8.068` and pgvector
  `0.063/307.782/32.749` for BM25/dense/hybrid. The dense row includes lazy
  model startup on its first timed query; the hybrid row better represents
  steady-state store overhead.
- **Alternative:** Treat any approximate-store delta as an implementation bug
  without first comparing against the exact baseline.
- **Why:** HNSW is approximate by design. The exact memory result defines the
  reference; quality differences must be measured and investigated rather than
  presumed incorrect.

## Phase 6a — OpenAI provider boundary

### Use the official Responses SDK behind the existing protocol

- **Decision:** Implement `LLMProvider` with the official asynchronous OpenAI
  client and the Responses API, passing the provider-neutral `LLM_API_KEY`
  explicitly to the client. Disable SDK transport retries and bound structured
  validation to one retry.
- **Alternative:** Let the SDK discover `OPENAI_API_KEY`, use Chat Completions,
  or add a framework-level provider abstraction.
- **Why:** Explicit key injection prevents an unrelated ambient credential from
  being selected silently. The existing protocol already isolates the provider,
  and fixed retry bounds plus `max_output_tokens` prevent accidental spend.

### Make effort, usage, and cost observable

- **Decision:** Send `reasoning.effort` on every request and record model,
  effort, ordinary/cached/cache-write input, output/reasoning tokens, and
  latency. Use the 2026-09-20 Standard-processing Luna/Terra price table and
  count reasoning only as part of output.
- **Alternative:** Depend on model defaults or estimate usage by locally
  tokenizing prompts.
- **Why:** The GPT-5.6 default effort can add invisible output-token cost. API
  usage is the billing source of truth, and separating cache writes from reads
  makes prompt-cache economics measurable.

### Keep the stable prompt prefix cacheable

- **Decision:** Place versioned developer instructions and a deterministic,
  `poi_id`-sorted catalog summary before one explicit cache breakpoint. Put all
  changing user content and retry feedback after it.
- **Alternative:** Interleave state and instructions or rely on the API's
  implicit breakpoint.
- **Why:** Exact prefix reuse is inspectable and preserves the intended cache
  boundary across retries. The checkpoint smoke prefix was 8,634 characters;
  both recorded calls wrote cache entries, while neither observed a cache read.

### Make replay the fail-closed default

- **Decision:** Default `LLM_MODE` to `replay`. Fixture identity includes call,
  prompt version, requested model/effort, output schema, and stable/dynamic
  hashes. Missing fixtures never create a client or fall through to live mode.
- **Alternative:** Default to live or automatically record a missing fixture.
- **Why:** Offline tests remain deterministic and stochastic model changes are
  reviewed explicitly. The two checkpoint fixtures used two live calls and
  cost `$0.00141200` in total.

## Phase 6b — Turn understanding

### Keep understanding as extraction at zero reasoning effort

- **Decision:** Send one user turn plus canonical `TripState` JSON to Luna as
  strict `TurnAnalysis` extraction with `reasoning.effort=none`. Normalize the
  Pydantic discriminated plan-edit union from `oneOf` plus `discriminator` to
  the Responses Structured Outputs subset's supported `anyOf`, while retaining
  full Pydantic validation after the response.
- **Alternative:** Ask the model to reason over a transcript, let it construct
  plans, or pay for nonzero reasoning effort on a constrained extraction task.
- **Why:** `TripState` is the durable conversation state and deterministic code
  owns planning. The first recording request exposed the schema-subset mismatch
  and returned no usage; after normalization, all five bounded extraction calls
  produced valid `TurnAnalysis` values at zero reasoning tokens.

### Fail conservatively without inventing operational facts

- **Decision:** Catch timeouts, refusals, transport failures, and exhausted
  structured-output retries at the use-case boundary. Return an observable
  `used_fallback=true` result whose deterministic English/Greek classifier only
  preserves safety-critical intent routes and literal, catalog-verified POI
  matches. Unknown names remain unresolved and no plan edit is fabricated.
- **Alternative:** Raise provider failures into orchestration, silently coerce
  malformed output, or guess POI ids and plan changes from partial text.
- **Why:** Routing can continue safely during provider failure without turning
  untrusted or malformed model output into state changes. The fallback is
  intentionally less helpful than the model, but never silently authoritative.

### Measure prompt-cache reuse on the extraction fixtures

- **Decision:** Keep the understand instruction and catalog prefix identical
  across cases and record API token details rather than infer cache behavior.
- **Alternative:** Assume the cache breakpoint works from request construction
  alone.
- **Why:** The first successful understand call wrote 4,398 prefix tokens; the
  next four each read all 4,398 tokens from cache. This confirms that changing
  `TripState` and user input remain entirely after the stable boundary.

## Phase 6c — Deterministic routing policy

### Keep tool selection out of the model boundary

- **Decision:** Route validated `TurnAnalysis`, the current user turn, and
  `TripState` through an exhaustive synchronous intent-to-tool table. Mixed
  intents take a canonical ordered union, and the router accepts no model-authored
  tool names or provider dependency.
- **Alternative:** Ask the LLM to select tools or silently treat a newly added
  intent as requiring no tools.
- **Why:** Tool choice is application policy. The frozen router eval has one
  case for every `Intent`, and both the evaluator and parameterized test fail
  when the enum and table differ.

### Override every plan touch with the complete safety chain

- **Decision:** Any plan intent, plan-edit delta, planning-constraint delta, or
  normalized English/Greek plan language requires catalog, weather, hours,
  travel, planner, and validator in that order. This union applies even when
  the model labels an obvious plan edit as `tourism_qa`.
- **Alternative:** Trust the extracted intent or reuse an old plan without
  rerunning its operational inputs and validator.
- **Why:** Misclassification cannot bypass weather, hours, travel, planning, or
  validation. The execution guard also stops on missing planner output or an
  invalid `ValidationResult`, before a narrator can receive the itinerary.

### Resolve the Phase 5c opening-hours failure at the source boundary

- **Decision:** Deterministic English/Greek hours and price guards override a
  conflicting tourism/recommendation classification. Hours route to catalog
  plus the hours engine; prices route to catalog; both make zero RAG calls.
- **Alternative:** Continue tuning the Phase 5c retrieval abstention threshold
  until the hours query happens to fall below it.
- **Why:** The exact gold query, “What are the White Tower opening hours?”, was
  semantically too close to descriptive White Tower content. This was a
  **source-boundary problem, not a retrieval-threshold problem**. The router is
  the architectural resolution: operational data stays in the catalog/hours
  boundary and never enters descriptive RAG.

## Phase 6d — Grounded narration and post-checks

### Let the model write prose, never a fact

- **Decision:** Narration emits controlled tokens instead of values:
  `{{poi:<poi_id>}}`, `{{time:<position>:<start|end>}}`, `{{cite:<evidence_id>}}`,
  and `{{fact:<evidence_id>}}`. Deterministic code substitutes canonical catalog
  names, exact validated-plan timestamps, numbered citation labels, and verbatim
  operational statements, and only after the post-check passes.
- **Alternative:** Let the model write names and clock times directly and check
  the finished prose against the plan by string comparison.
- **Why:** Checking free prose means guessing which substring was meant to be a
  fact. With tokens, every factual substring in a final answer is inserted by
  code from a named source, so the audit is exact rather than heuristic.

### Add a `fact` token rather than ban operational answers

- **Decision:** Extend the four specified token kinds with `{{fact:<evidence_id>}}`,
  which renders one operational statement exactly as the catalog or hours engine
  returned it, and which the post-check rejects when it points at retrieved text.
- **Alternative:** Keep only `poi`, `time`, and `cite`.
- **Why:** An hours or price answer has no plan positions to point at, so with
  only those three kinds the model could not state a catalog opening time at all
  without writing a raw clock time — which it is forbidden to author. Every such
  answer would have fallen to the template. The decision was taken before the
  pass-rate gate was measured, not to rescue it afterwards.

### Reject first, retry once, then template

- **Decision:** Post-check the raw draft for unknown or out-of-plan POI ids,
  time positions outside the plan, citations absent from this call's evidence
  registry, raw clock times, raw catalog names, unreviewed proper names,
  ungrounded prices and continuous-opening claims, instruction-like text, and
  wording copied from an untrusted chunk. On failure, retry exactly once with
  the stable prefix untouched and a sanitized violation list appended after the
  cache breakpoint. On a second failure, a provider failure, or a refusal, render
  the deterministic template and report `used_fallback` with violation codes.
- **Alternative:** Retry until the draft passes, or return the draft with a
  warning.
- **Why:** Unbounded retries turn a prompt problem into an unbounded bill, and a
  warned-about wrong answer is still a wrong answer. The template is written in
  the same token language and rendered by the same renderer, so the fallback path
  cannot introduce a fact the checked path would have rejected.

### Measure the rejection rate before trusting the checks

- **Decision:** Record the five frozen narration cases once with Luna and once
  with Terra and report the first-draft pass rate before writing the rest of the
  suite. Gate: at least 4/5 per model.
- **Result:** Luna 4/5, Terra 5/5, no template fallbacks in either run. Luna's
  single rejection was `FACT_IS_NOT_OPERATIONAL` twice in one draft — it used
  `{{fact:...}}` on two RAG chunk ids — and its retry passed. Eleven live calls,
  `$0.03979775` in total.
- **Why:** Strict checks that reject normal drafts produce a robotic demo. That
  had to be measured on real output, not assumed, and measured before the tests
  could be shaped around whatever the models happened to do.

### Default narration to Luna

- **Decision:** Keep `NARRATE_MODEL=gpt-5.6-luna` at `reasoning.effort=low`.
- **Observed:** Over the same five cases, Luna cost `$0.00482665` including its
  one retry; Terra cost `$0.03497110`, about 7.2 times more, for one additional
  first-draft pass. Both produced grounded, citable answers on every case.
- **Why:** The retry and template machinery exists precisely to absorb Luna's
  occasional rejection, and it did so for a fraction of Terra's price. Terra
  stays configurable for a quality comparison.

### The validated plan is the only schedule truth

- **Decision:** The narrator receives a `NarrationBundle` that refuses to
  construct when a plan is present without a passing `ValidationResult`. The
  bundle is read-only: narration returns text and never a plan. Tests assert the
  plan JSON is byte-identical before and after narration, and the eval raises if
  any case's plan JSON changes.
- **Alternative:** Let narration adjust or annotate the itinerary.
- **Why:** Any edit at narration time would bypass the validator, which is the
  one component that recomputes everything from scratch.

### Prompt-cache behavior on narration

- **Observed:** The narration stable prefix is 3,467 tokens. Each model wrote it
  once and then read all 3,467 from cache on every later call, including the
  retry. Per-call dynamic input ranged from 372 to 2,622 ordinary input tokens.
- **Why it matters:** The violation feedback is appended after the breakpoint, so
  a retry costs one short suffix rather than a second full prefix.

### Known limitations, disclosed not fixed

- **Fact statements read as fragments.** Because `{{fact:...}}` emits the catalog
  statement verbatim, a model lead-in can join it ungrammatically, as in "The
  first stop is open during the scheduled visit open from 08:00 to 20:00…".
  This is a wording problem, not a grounding problem. Fixing it means rephrasing
  the fact statements, which changes the dynamic-input hash and invalidates the
  recorded fixtures, so it is left for a future prompt version.
- **The renderer is language-blind.** `{{poi:...}}` always renders `names.en`,
  so a Greek-language answer reads "στο Hagios Demetrios" rather than using the
  Greek catalog name. The catalog carries `names.el`; selecting it needs a turn
  language, which arrives with the pipeline in 6e.

## Phase 6e — Conversation pipeline

### Sequence the turn explicitly, in one place

- **Decision:** `ConversationPipeline.run_turn` executes understand → route →
  gather → plan → validate → narrate → post-check as seven named stages it
  records and returns. The endpoint and the CLI own no sequencing; they inject
  `now` and hand back state.
- **Alternative:** Let a model drive tool calls, or spread the order across
  framework callbacks and request handlers.
- **Why:** The order *is* the safety argument — narration cannot run before the
  validator passes. An order hidden in callbacks cannot be asserted; a recorded
  stage list can, and `test_pipeline_runs_stages_in_required_order` does. The
  post-check is injected into the narrator per call so it shows up as its own
  observed stage rather than being assumed.

### `TripState` is the memory; there is no transcript

- **Decision:** A turn receives the current user text, a validated `TripState`,
  and a server-injected `now`. No prior user turn, no prior assistant prose, no
  message list reaches the model or the pipeline. `/chat` has no session store:
  the client returns the state it was given.
- **Alternative:** Accumulate a chat history and send it with every request.
- **Why:** Prompt size then grows with conversation length, and a past narration
  error becomes durable context. With structured state, the per-turn overhead is
  constant: `test_prompt_size_does_not_grow_with_turn_count` runs six turns and
  asserts the prompt length minus the turn text is a single value. Follow-up
  meaning comes from the window, interests, exclusions, party, and itinerary
  version instead.

### Commit plan changes transactionally

- **Decision:** Constraint updates are applied to a working copy; gather,
  planning, and validation run against that copy; the itinerary and its version
  are committed exactly once, only after the independent validator passes. A
  failure returns typed violations, the unchanged state, and a deterministic
  message — never narration.
- **Alternative:** Mutate state as the turn proceeds, or narrate a plan with a
  warning attached.
- **Why:** Otherwise a rejected turn can leave new constraints sitting beside an
  old itinerary that those constraints invalidate. The previous plan is
  rehydrated from stored state by re-validating it, which also enforces that
  state only ever holds plans that still pass.

### Resolve dates and answer language deterministically

- **Decision:** The model may quote "tomorrow" in `requested_date_text`; it never
  resolves it. `resolve_time_window` normalizes the injected `now` to
  `Europe/Athens` and computes the window, recording the assumed start hour as a
  stated assumption. Answer language is resolved from the script the traveler
  typed, before any model call, and the renderer selects `names.el` or
  `names.en` from it.
- **Alternative:** Ask the model for a date, or infer language from the model's
  own output.
- **Why:** Both are presentation and arithmetic, not understanding. The test
  injects 23:30 UTC on the 21st — already 02:30 Athens on the 22nd — and asserts
  "tomorrow" resolves to the 23rd, which the system clock could not produce.

### No scored user interface

- **Decision:** A JSON `POST /chat` and a printing CLI, no HTML, CSS, JS,
  streaming, or auth.
- **Why:** The assignment scores the UI at zero. Both surfaces exist only to show
  the same pipeline running end to end, so effort went into the sequencing, the
  state contract, and the tests instead.

### Carried-over 6d defects fixed in one prompt bump

- **Decision:** `narrate.v2` fixes both defects together: the renderer now emits
  Greek catalog names, Greek source labels, and a translated fallback template
  for a Greek turn; and operational statements are phrased as standalone clauses
  ("Opening hours on 2026-09-22 are 09:00 to 17:00, with last entry at 16:40")
  with a prompt rule forbidding a lead-in before a `{{fact:...}}` token.
- **Cost:** A prompt change alters the stable-prefix hash, so every narration
  fixture had to be re-recorded. Within the available budget that bought Luna,
  the configured default, and the three conversation turns. Terra's 5/5
  narrate.v1 measurement stands as reported evidence; re-recording it is
  `python -m evals.narration_report --record --model terra`.
- **Result:** Luna's first-draft post-check pass rate rose from 4/5 on v1 to 5/5
  on v2, and the assignment scenario needed no retries and no fallback.

### The template must not echo a planner-generated area label

- **Decision:** A break or area meal renders as a neutral localized phrase
  ("a rest stop"), not the planner's `area_label`.
- **Why:** The planner writes labels like "Indoor rest near rotunda", which embed
  a place name. A raw place name outside a token is exactly what the post-check
  rejects, so the fallback template could fail its own checks on any plan
  containing a break.

### Known defects, disclosed not fixed

- **`OPERATIONAL_FACTS` is misnamed.** It carries every trusted evidence item,
  including RAG chunks, and offers each a `{{fact:...}}` token that the
  post-check then rejects for non-operational evidence. `AVAILABLE_TOKENS`
  already lists the correct set, so the contract is sound, but the prompt sends
  two signals. This produced Luna's only narrate.v1 rejection. The fix is to
  split the block at `narrate.v3`; it is deferred because it invalidates every
  fixture.
- **Facts are listed without their subject.** An hours statement does not name
  its POI — it cannot, since names are tokens — so Luna batches them into a
  trailing block that does not say which stop each belongs to. The fix is a
  prompt rule requiring each fact adjacent to its `{{poi:...}}` token. Also
  deferred to `narrate.v3` for the same reason.
- **Operational statements are English only.** A Greek turn gets Greek prose,
  Greek place names, and a Greek fallback, but the hours and price clauses stay
  in English. Localizing them means translating catalog-derived text, which is a
  data-layer change rather than a rendering one.
- **Greek names are rendered in the nominative.** "στο Ιερός Ναός Αγίου
  Δημητρίου" should decline. The catalog stores one form per language; declension
  would need a second field per case.

## Phase 7 — End-to-end evaluation harness

**Repetitions are sampled once, at record time.** A pass rate over three runs
and a byte-reproducible offline suite are only compatible if model variance is
frozen. Repetition `k` of a case gets its own narration fixture namespace
(`case_<id>_rep<k>_narrate`), so `make eval-full` replays genuinely different
model samples and CI reports the same number every time. Understanding is
recorded once per case and replayed for later repetitions, which makes an extra
repetition cost one live call instead of two.

**No LLM-as-judge.** The two things a judge would score — groundedness and
helpfulness — are exactly what this design refuses to let a model decide, and a
judge would cost more live calls than the whole harness. The measured metrics
are all computed by code. Groundedness and helpfulness need human review, and
the design note should say so rather than imply the harness covers them.

**A failing case is a finding.** No case was rewritten, softened or deleted to
make a run green. Seven of twenty cases fail at least one repetition; all seven
share one root cause, recorded below.

### Finding 1 — the temporal resolver turns any dated question into a planning turn

`ConversationPipeline.run_turn` folds the deterministically resolved time
window into `analysis.constraint_updates` *before* calling `route_turn`, and
`router.touches_plan` treats any constraint update as a plan touch. So any turn
containing "tomorrow" or "today" routes through the planner and commits an
itinerary, whatever the traveler actually asked. Demonstrated in isolation: the
same analysis gives `touches_plan=False` before the fold and `True` after it.

Measured effect: asked "What concerts are on in Thessaloniki tonight?" the
assistant answers correctly in prose *and* silently commits itinerary version 1.
The same happens for the weather, opening-hours, public-holiday and safety
cases. The prose is right; the state change is not.

Not fixed in Phase 7, which adds no product behaviour. The fix is to route on
the analysis the model returned and fold the resolved window in only once the
route has decided it touches the plan.

### Finding 2 — a feasibility check answers a different question than it was asked

Asked whether a 16:45 arrival works for a 17:00 closing time, the pipeline
plans a different, feasible day and commits it rather than reporting the
infeasibility. The prose answer correctly says "No", so the failure is in the
state, not the words. Same root cause as Finding 1.

### Finding 3 — weather questions have no citable forecast

`orchestrator/evidence.weather_summary_evidence` is defined and never called,
so a weather turn reaches narration with no forecast in its evidence registry.
The model then correctly refuses: "Weather information for tomorrow is not
available in the supplied details." Wiring it changes the dynamic input of
every plan-touching turn and would invalidate the Phase 6e conversation
fixtures, so it is recorded here rather than fixed mid-checkpoint.

### Two crashes fixed, because they blocked measurement

- `LazyRetriever._build` imported `dense_encoder_from_env` from `app.rag.dense`;
  it lives in `app.rag.retriever`. No Phase 6 test routed the pipeline to
  retrieval — every conversation turn goes to the planner — so the wiring had
  never been executed.
- `BeamSearchPlanner` raises `ValueError("planning requires a user time window")`.
  A turn routed to planning with no window let that escape `run_turn`, which is
  a 500 from `/chat`. The plan stage now skips instead, and the turn answers in
  prose.

Both are regressions covered by tests. Nothing else in `app/` changed except
additive observability fields on `PipelineResult` that the harness reads.

### Understand effort: `none` versus `low`

Intent accuracy is identical, 5/5 both ways, which is the evidence the Phase 6
spec asked for, so the default stays `none`. Full-analysis agreement is 4/5,
and the one disagreement is worth recording: given "Plan a five-hour history
visit tomorrow", `effort=none` emitted
`time_window: 0900-01-01T00:00+02:00 .. 05:00` — it encoded "five hours" as an
absolute datetime range in the year 900 — while `effort=low` correctly left the
field unset. The deterministic resolver overwrites that window, so the bad value
never reaches the planner.

That is the "code decides time" thesis paying for itself under measurement
rather than in argument. It is also the strongest available argument for
validating the model's `time_window` explicitly instead of relying on the
resolver to overwrite it. `low` costs 13% more output-plus-reasoning tokens
(894 vs 789) for no gain in intent accuracy.

### Budget

78 live calls authorised, 72 used. Seven were spent twice because two harness
defects recorded narration against a degraded analysis: the provider refuses to
overwrite a fixture, the pipeline turns that refusal into an ordinary provider
failure, and the harness was reading narration's `failure_category` when it
needed understanding's. Both are now guarded, and `PipelineResult` carries
`understand_failure_category` so the two stages are distinguishable. The lost
calls meant repetition 3 no longer fit, so every case reports a pass rate over
two recorded samples rather than three; the denominator is printed per case.

## Feasibility questions are read-only and deterministic

Phase 7 Finding 2 recorded that a `feasibility_check` turn reached the beam
planner, which built a different feasible day and committed it, so the prose
said "no" while the state said "yes". This closes that finding.

### Decision

A turn whose intents include `feasibility_check` and neither `create_plan` nor
`edit_plan`, and which carries no plan edits, takes its own path:

- The planner and validator are not called. The router's tool matrix is
  unchanged; the pipeline drops those two tools from the route for this path
  and reports `feasibility` as the tool and stage that ran instead.
- The traveler's arrival time or time range is parsed into a feasibility window
  (`resolve_feasibility_window`). A time preceded by "close/closes/closing" is a
  claim about the venue, not part of the window: the opening-hours engine is the
  authority on it. With one time and no end, the rest of that day is treated as
  available and the assumption is recorded.
- `FeasibilityChecker` evaluates the requested POIs (or the stored plan's stops
  when the turn names none) and returns a verdict plus findings, which are
  surfaced as typed violations.
- The answer is rendered from that result by a deterministic template in
  English or Greek. Narration and the post-check are skipped, so a model cannot
  contradict the verdict.
- Nothing is committed: the itinerary and `itinerary_version` are unchanged.
- If the place or the time is missing, the turn returns a clarification request
  instead of falling back to speculative planning.

### Reason

A feasibility question is a query, not a state mutation, and its correctness
should not depend on generative model behavior. The checker is exhaustive and
already tested; putting a planner in front of it answered a different question
and putting a model after it allowed a contradiction. The cost is a templated,
less conversational answer, which is acceptable for a yes/no verdict with
concrete times.

### Consequences

- `feasibility_feasible` no longer commits a plan; its eval expectations changed
  to match (no commit, version 0, answer starts with "Yes").
- The deterministic Greek feasibility templates received a manual native-language
  review, avoid articles because catalog names are nominative with varying
  grammatical gender, and are covered by tests.
