# Technical Design Note
## Thessaloniki Tourist AI Assistant

---

## 1. The idea the whole system is built on

A tourist assistant fails in a specific way. It produces an answer that reads
beautifully and is wrong in a manner the reader cannot detect: a museum that
closed an hour ago, a walk that takes forty minutes presented as fifteen, a
route that crosses the city three times.

Language models are good at language and bad at arithmetic and geometry. So the
system is organised around one rule:

> **The LLM understands and narrates. Code solves and verifies.**

Concretely:

| Decision | Owner |
|---|---|
| What the user meant, which constraints changed | LLM (structured extraction) |
| Descriptive and historical content | RAG, with citations |
| Weather | Live API (Open-Meteo) |
| Current time, opening hours, travel times | Deterministic code |
| Which itinerary to build | Deterministic planner |
| Whether that itinerary is valid | Independent validator |
| Which tools run on this turn | Deterministic router |
| Turning a validated plan into prose | LLM (narration only) |
| Opening hours, prices, events, travel times | **Never guessed by the LLM** |

Every architectural decision below follows from this split, and the evaluation
in §7 measures whether the split actually holds under load.

---

## 2. Architecture

A turn runs through seven stages, always in this order:

```
understand → route → gather → plan → validate → narrate → post-check
```

**Understand** is one structured LLM call. It receives the user turn plus the
current `TripState` and returns a `TurnAnalysis`: intent, entities, constraint
deltas, plan-edit operations. It produces no plan, answers no question, calls no
tool.

**Route** is a pure synchronous function. It maps intent to a mandatory tool
list, and it is not advisory. Any turn that touches the plan runs the complete
chain — weather, hours, travel, planner, validator — regardless of what the
model's analysis said. The router never constructs an LLM client; a test
asserts this by replacing the provider constructor with one that raises.

**Gather** calls only the tools the router selected.

**Plan** runs beam search over filtered, scored candidates.

**Validate** is fully independent of the planner. It recomputes everything from
the catalog, matrix, hours engine and weather flags, and explicitly ignores any
times already stored in an itinerary. Neither the planner nor a future LLM edit
can smuggle wrong timing past it.

**Narrate** is the second LLM call. It receives the validated plan JSON and
nothing else that matters. It cannot alter the plan.

**Post-check** is deterministic and described in §6.

State between turns is the `TripState` object, never the raw transcript. A test
proves prompt length minus the current turn's text is constant across six turns,
so context does not grow without bound.

---

## 3. Knowledge and RAG

### The source boundary

The knowledge base is split in two, and the split is the most important design
decision in the retrieval layer:

- **`data/pois.yaml`** — the operational catalog. Opening hours, coordinates,
  categories, aliases, temporary closures, date overrides, holiday rules.
  Structured, versioned, verifiable.
- **`data/content/*.md`** — descriptive prose. History, what to see, practical
  tips. One document per POI, sectioned.

Content documents contain no opening hours and no prices. This is enforced by
tests, not by discipline: a regex boundary test rejects clock-time patterns,
currency patterns, and internal system vocabulary such as "catalog" or
"structured", with historical years and centuries explicitly allowed as positive
controls. The vocabulary check was added after a real leak — three documents had
sentences telling the reader that operational details come from the structured
catalog, which is both meaningless to a tourist and noise in the index.

### Pipeline

Ingestion chunks by `##` section with stable ids (`<poi_id>#<section-slug>`) and
content hashes. Re-ingesting unchanged content is a no-op. The corpus is 88
chunks: 22 POIs × 3 descriptive sections, plus one **alias chunk** per POI
carrying every Greek and English name from the catalog. That alias chunk is what
lets "Καμάρα" retrieve the Arch of Galerius lexically without bilingual body
text.

Greek normalisation is NFD plus combining-mark removal (strips τόνοι), final
sigma folding (`ς` → `σ`), lowercasing and punctuation stripping. No stemming:
for a small bilingual corpus dominated by proper nouns, transparent
spelling-equivalence beats an opaque analyser.

Retrieval is hybrid: BM25 (implemented directly, k1=1.5, b=0.75, ~50 lines, no
library) fused with dense e5-small embeddings via Reciprocal Rank Fusion at
k=60.

### Measured retrieval quality

| Mode | recall@5 | recall@5 (POI-deduped) | MRR | mean latency |
|---|---|---|---|---|
| BM25 | 0.852 | 0.870 | 0.862 | 0.06 ms |
| Dense | 0.963 | 0.981 | 1.000 | (includes model load) |
| Hybrid (RRF) | 0.944 | 0.963 | 0.917 | 8.1 ms |

**Hybrid is worse than dense here, and that result is kept rather than tuned
away.** In an 88-chunk corpus IDF is weak, the lexical half contributes noise,
and RRF fuses by rank without weighting by quality. At a larger corpus the
balance would shift. All three modes remain in the harness so the claim stays
falsifiable.

A separate intervention is worth recording: adding Greek-script aliases to
`pois.yaml` moved lexical recall from 0.815 to 0.907 on the gold set as it stood
at that point, with both measurements preserved verbatim in `evals/results/`. No
algorithm changed; only data did. Greek queries had previously returned zero
BM25 hits because no Greek token existed anywhere in the index.

### The abstention finding

The design intended a calibrated abstention threshold: refuse to answer when the
best dense cosine and the best normalised BM25 score both fall below a floor.
Calibration was run as a sweep over the gold set.

**No threshold pair separates the classes.** Rejecting "What are the White Tower
opening hours?" requires a dense floor above 0.855, which would also reject a
legitimate in-KB Greek query about Heptapyrgio scoring 0.838. The thresholds were
left unset and the failure reported rather than a plausible-looking cutoff
chosen.

The reason is structural, and understanding it changed the architecture. The
query is *semantically* in-domain — the White Tower is thoroughly in the
knowledge base. Only the *data type* is out of domain: hours are catalog data,
not prose. Similarity cannot separate topic from data type, because they are not
the same axis.

This was a source-boundary problem, not a threshold-tuning problem. It is
resolved in the router (§4), which sends opening-hours and price questions to
the catalog and never to retrieval. A spy-verified test using that exact gold
query asserts zero retriever calls.

### Storage

A `VectorStore` Protocol has two implementations. `InMemoryVectorStore` is exact
numpy brute force and is the reference baseline and the test double.
`PgVectorStore` is raw parameterised SQL against Postgres with pgvector, an HNSW
index, and a mandatory `city_id` filter on every call.

The application defaults to pgvector; tests always use in-memory, so the suite
never depends on a running database. Quality delta between the two: zero.
Latency cost: 8 ms to 33 ms on the hybrid path.

Two measurements worth stating plainly:

- At 88 rows Postgres chooses a sequential scan over the HNSW index, because it
  is cheaper. A synthetic sweep found the planner first selecting HNSW at
  approximately 2,250 rows. The index is correct to have and currently unused.
- pgvector applies `WHERE city_id` *after* the index collects `ef_search`
  candidates, so a filtered search can under-fill `top_k` even when enough
  matching rows exist. At multi-city scale this needs iterative index scans
  (pgvector 0.8+) or per-city partial indexes. Not implemented; documented.

---

## 4. Tool orchestration

The router is a table, as code data, not as prompt text:

| Intent | Mandatory tools, in order |
|---|---|
| `tourism_qa`, `recommendation` | catalog, retriever |
| `create_plan`, `edit_plan`, `feasibility_check` | catalog, weather, hours, travel, planner, validator |
| `weather_question` | weather |
| `opening_hours_question` | catalog, hours |
| `price_question` | catalog |
| `safety` | catalog, weather |
| `unsupported_live_info`, `out_of_scope`, `small_talk` | none |

Two overrides sit above the table:

**Plan-safety override.** `touches_plan` is computed deterministically from the
intent, any non-empty plan edits, any planning-constraint change, or normalised
English/Greek plan language in the user's own text. When true, the full planning
chain is unioned in even if the model classified the turn as something else. A
test feeds a `TurnAnalysis` saying `tourism_qa` alongside user text that plainly
asks to change the plan, and asserts the complete chain still runs in order.

**Source-boundary guard.** Opening-hours and price language routes to the
catalog before the intent table is consulted, overriding a conflicting
`tourism_qa` analysis.

If the planner returns nothing, or the validator returns invalid, the route
stops before narration and returns typed violations. The narrator never receives
an unvalidated itinerary.

A parameterised test covers every `Intent` value, so adding an intent without a
routing rule fails the suite rather than silently receiving no tools.

---

## 5. Itinerary feasibility

Feasibility is arithmetic, so code does it.

`feasibility.py` brute-forces all orderings for up to six stops and produces two
estimates: a **lower bound** using minimum visit durations, and a **comfortable**
estimate using typical durations scaled by pace factors. It returns a verdict of
`feasible | tight | infeasible`, a numeric breakdown (visit minutes, walking
minutes, buffers, margin to deadline), and **minimal fixes** — drop this specific
stop, start twenty minutes earlier. It never invents a faster transport mode to
make an answer fit.

Travel times come from a real 22×22 OpenRouteService foot-routing matrix.
Validator and feasibility unit tests use a small fixed fixture matrix with
hand-computed expectations, never the live file, so regenerating routing data is
a data change rather than a phantom code regression.

The opening-hours engine is DST-safe, tested across the October 2026 Athens
transition, and resolves a precedence chain: temporary closure > date override >
per-POI holiday > seasonal schedule. It checks last entry and whether the full
visit fits, not merely whether the door is open at the start time.

**Valid and good are different layers.** An early planner passed every validity
test and produced poor plans: 3.5 of 5 available hours used, lunch at 11:53,
weather edits that deleted activities instead of reordering them, and drop
reasons that were simply false — one candidate reported as `LOWER_SCORE` with a
*positive* score gap. A separate quality-assertion layer was added on top of
validity, and weather repair was reordered to prefer reorder → swap → fill →
remove, with removal as the last resort.

A shared `rules.py` between planner and validator is an accepted risk. The
mitigation is that validator tests use hand-computed expected values rather than
values derived through the shared helpers, so a shared bug cannot hide inside its
own test suite.

---

## 6. Reliability and hallucination control

### Model and prompt design

`gpt-5.6-luna` for both understanding and narration, via the Responses API.
Selection criteria were multilingual capability (Greek), structured-output
support, cost and latency. `reasoning.effort` is set explicitly on every call and
never left at the default, because reasoning tokens bill as output tokens.

Understanding runs at `effort=none`. This was measured, not assumed: intent
accuracy was identical at `none` and `low` (5/5 both ways), and `low` cost 13%
more tokens for no gain.

The comparison produced a better finding than the headline. At `effort=none` the
model, given "Plan a five-hour history visit tomorrow", encoded "five hours" as
a datetime range **in the year 900**. The deterministic time resolver overwrote
it, so the value never reached the planner. That is the central thesis paying for
itself under measurement rather than in argument.

Narration was compared across models: `gpt-5.6-luna` passed post-checks on first
draft 4 of 5 times, `gpt-5.6-terra` 5 of 5, at 7.2× the cost. Luna was kept.

Every prompt is ordered stable-prefix-first with an explicit cache breakpoint.
The measured effect: first call $0.00134, subsequent cached calls $0.00028, a
4.8× reduction, with 4,398 prefix tokens read from cache on every later call.

### Controlled-token narration

The narrator does not write facts. It writes tokens:

```
{{poi:<poi_id>}}   {{time:<index>:start}}   {{cite:<evidence_id>}}   {{fact:<evidence_id>}}
```

Deterministic code substitutes canonical catalog names, exact plan times and
citation labels, and only after every post-check passes. Model output containing
a raw clock time or a raw POI name is rejected.

Post-checks, all deterministic:

1. Every POI token names a `poi_id` in the catalog *and* in the current plan or
   evidence bundle. A conservative proper-name scanner rejects unrecognised
   place-like names outside a small reviewed allowlist.
2. Every time token resolves to an existing activity boundary, so the rendered
   timestamp is by construction the one in the validated plan JSON.
3. Every citation id exists in this call's evidence registry. Existing somewhere
   in the corpus is not enough.
4. No unresolved token, no instruction-like text copied from retrieved content,
   no catalog-only operational claim attributed to RAG.

On failure: exactly one retry with a sanitised violation list appended after the
cache breakpoint, post-checked from scratch. On second failure, provider refusal
or timeout: a deterministic template rendered directly from the validated plan.
A user may receive a plain answer. A user never receives a silently wrong one.

### The number that matters

| Metric | Value |
|---|---|
| Entity hallucination, **first draft** | 12.2% |
| Entity hallucination, **delivered answer** | **0%** |
| Citation validity | 100% |
| Validator violation rate | 0% |
| Post-check first-draft pass rate | 87.5% |
| Template fallback rate | 4.2% |

The model hallucinates. The architecture stops it before the user sees it. That
gap between 12.2% and 0% is the design working.

### Prompt injection

Retrieved documents are untrusted data, fenced in a delimited block after the
cache breakpoint, never promoted to developer instructions. A deliberately
poisoned document lives in `evals/fixtures/injection/`, outside `data/`, and
carries an instruction to ignore prior sources and assert a venue is always open.
The test feeds it through the real pipeline: the draft that obeys it is rejected
with typed violations, and the delivered answer contains no price, no "always
open", and no time outside the plan.

### Safety-critical recommendations

Safety-critical outdoor destinations are treated differently from ordinary
sightseeing. Seich Sou forest is excluded outright when weather is unavailable,
because **missing weather is never treated as safe weather**. This invariant was
set when the weather layer was built and holds through every later layer; a
tool failure must never read as an absence of risk.

### Source precedence, from real incidents

Two conflicts occurred in this project's own data pipeline and are the basis for
the precedence rules:

- **Archaeological Museum.** A first draft used a secondary source giving
  seasonal hours of 08:00–20:00 summer / 09:00–16:00 winter. The official site
  gives 09:00–17:00 year-round. Manual verification caught it; no code would
  have. This is exactly the incident the assignment's postmortem exercise
  describes, and it happened here.
- **Jewish Museum.** Official sites give Monday–Friday 09:00–14:00; Google Maps
  says Monday closed. The conservative rule applies: Monday is recorded as
  **unknown**, not as open and not as confidently closed, and the conflict is
  recorded.

A third case is a localisation trap worth naming: Greek listings for Modiano
Market show "12:00 π.μ.", which means **midnight**, not noon.

Every uncertain field carries `needs_verification`, a source kind and a
confidence level, with a review queue in `docs/data-verification.md`.

---

## 7. Evaluation

`evals/cases.yaml` holds 20 cases: factual Q&A with citation, personalised
recommendation, plan creation, category exclusion, the three-turn continuity
scenario at conversation level, weather replan, heat, positional edit, feasible
and infeasible feasibility checks, last-entry edge case, public-holiday edge
case, unsupported live info, a safety-critical storm case, an adversarial
instruction, and a Greek-language variant.

Each case runs multiple times and reports a **pass rate**, not a single
pass/fail, because model output is stochastic. `now` and the weather fixture are
frozen per case. Repetition *k* gets its own fixture namespace, so replaying the
suite exercises genuinely different model samples while staying offline and
deterministic.

Automated: tool-selection accuracy, validator violation rate, entity
hallucination, citation validity, post-check pass rate, fallback rate, retrieval
recall@5 and MRR, latency, tokens, cost. Requiring human review: groundedness
beyond entity checking, helpfulness, tone, whether a plan is *good* rather than
merely valid. No LLM-as-judge metric is reported, because an unvalidated judge
would add a number without adding evidence.

**The harness found a real bug that no unit test could.** Tool-selection accuracy
came in at 70.7%. The cause: the pipeline folded the deterministically resolved
time window into the analysis *before* routing, and the router treats any
constraint update as a plan touch. So any turn containing "tomorrow" routed
through the planner and committed an itinerary. Asked about concerts tonight, the
assistant answered correctly in prose and silently created a plan.

No Phase 6 test caught it, because every test turn was a planning turn. Routing
was changed to run on the model's own analysis with the window folded in
afterwards. Tool-selection accuracy went to **95.8%**, and the pass rate from
13/20 to 18/20.

Two failures remain and are reported rather than hidden:

- `feasibility_infeasible` still commits a plan and reports zero violations. This
  is a planner/validator issue, not routing.
- `personalised_recommendation` falls back to the deterministic template.

---

## 8. Production and scaling

**Destination isolation.** `city_id` is mandatory on every vector-store call and
indexed. One table, filtered — not a deployment per city. A test inserts two
cities and asserts a scoped search never returns the other. The known
post-filtering caveat in §3 is the first thing to fix at multi-city scale.

**Data freshness, by type.** Weather is live with a 20-minute cache and a 16-day
horizon check. Opening hours are versioned catalog data with `verified_at` and
source precedence; they need a re-verification SLA per source kind — official
sites quarterly, crowd-sourced listings more often and at lower confidence.
Descriptive content is nearly static. Events and ticket availability are not
implemented and are refused rather than guessed, which is the correct behaviour
for data the system cannot verify.

**Caching and cost.** Prompt caching gives the measured 4.8× reduction on
repeated prefixes. The walking matrix is precomputed, not called live. Embeddings
are cached by model name plus corpus content hash, so a content edit invalidates
them.

**Latency.** Roughly 2.5–3 seconds per LLM call, so 6–8 seconds for a full
planning turn. Acceptable for a prototype, not for production. The planner and
validator are milliseconds; the model calls dominate, and streaming narration
would be the first improvement.

**Observability.** Every LLM call records model id, effort, token breakdown
including cached and reasoning tokens, and latency. Each turn records its stage
trace and tool calls. A trace id would let any incident be replayed exactly,
which the record/replay harness already makes mechanically possible.

**What is deliberately not logged:** API keys, in any form, redacted from message
text, exception text, arguments, structured fields and SDK errors. Raw prompts
containing user text. Precise user location. Fixtures are scanned for
secret-shaped strings before commit.

**Security.** Retrieved content is untrusted input, delimited and post-checked.
All SQL is parameterised, never concatenated with user or retrieved text. Tool
arguments are validated by Pydantic schemas with `extra="forbid"`. Coordinates
should be bounding-box constrained per city, and outbound URLs allowlisted;
neither is implemented in the prototype.

---

## 9. Honest limitations

- **Window utilization counts idle time as used.** A plan with a 94-minute gap
  can report 95%. The fix was specified and not implemented, so the number is
  wrong in a known direction.
- **The content corpus is LLM-drafted**, every document at `confidence: draft`
  with empty `source_urls`. Six POIs have human-verified historical dating. The
  review queue is real and unfinished.
- **Gold sets and eval cases were largely authored alongside the corpus they
  test.** Queries may echo the vocabulary of the documents they are meant to
  find, which inflates recall in a way the table does not show. Independent
  human-written queries are the correct control and have not been run at scale.
- **`feasibility_infeasible` and `personalised_recommendation` fail**, as above.
- **Two of three abstention gold cases return empty by coincidence**, not by
  mechanism, since no abstention threshold is in force.
- **The project exceeded the 6–8 hour budget substantially.** The time went into
  the deterministic core, retrieval, and the record/replay harness. No hour count
  was kept.

---

## 10. Closing

The system is small enough to explain line by line and has no orchestration
framework in it. Every non-trivial decision, with the alternative considered and
the reason, is in `docs/decisions-log.md`.