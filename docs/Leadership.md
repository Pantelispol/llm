# Leadership and Coaching Plan
## Thessaloniki Tourist AI Assistant — production build

Team: two backend developers (B1, B2), one frontend/mobile developer (F), one
junior AI/ML developer (J). Technical lead: myself.

---

## 1. How I would run this

The prototype proved one thing worth keeping: the parts that must be correct are
deterministic and testable without a model. That shapes the whole plan. The two
backend developers own the parts where correctness is provable. The junior owns
the parts where correctness is *measurable*, which is the best place to learn.
I own the boundary between the two, because that is where this kind of system
fails.

Three rules apply from week one:

- **Nothing ships without an eval case.** A bug fix without a regression case is
  not finished.
- **The validator is never bypassed.** If a feature needs the validator relaxed,
  the feature is wrong.
- **Specs before code, in the repo.** Every non-trivial unit of work gets a short
  written spec with acceptance criteria. This kept the prototype coherent and it
  is how I review.

---

## 2. Six-to-eight week plan

### Weeks 1–2 — Foundations and contracts

| Who | Work |
|---|---|
| B1 | Domain schemas, API surface, session and preference store, CI |
| B2 | Catalog service, opening-hours engine, holiday and closure precedence |
| F | Chat UI skeleton, streaming, itinerary rendering from structured data |
| J | **Reads the prototype and writes 20 failure cases by hand.** No code. |
| Me | Architecture review, the LLM/deterministic boundary written down |

J's first task is deliberately not implementation. Before writing a planner,
they need to have *seen* twenty plausible-looking wrong itineraries.

**Milestone:** schemas frozen, CI green, catalog loading with verified hours.

### Weeks 3–4 — Planning core and live data

| Who | Work |
|---|---|
| B1 | Travel-time service, routing provider abstraction, caching |
| B2 | Weather integration, weather flags, freshness policy per data type |
| F | Plan editing UI, diff display, "what changed" affordance |
| J | **The validator.** Independent, recomputes everything, stable codes. |
| Me | Review J daily, pair on the first three violation codes |

The validator is the right first real contribution for a junior: the
specification is exact, correctness is checkable by hand, and it teaches the
central lesson of the system from the inside.

**Milestone:** a plan can be built, validated and shown. No LLM yet.

### Weeks 5–6 — LLM layer and orchestration

| Who | Work |
|---|---|
| B1 | Provider adapter, structured outputs, record/replay, cost telemetry |
| B2 | Router, mandatory tool policy, post-checks, fallback rendering |
| F | Multi-turn continuity, error and fallback states in the UI |
| J | RAG ingestion and retrieval evaluation, recall/MRR reporting |
| Me | Prompt architecture, red-teaming the injection surface |

**Milestone:** a full conversation turn runs end to end, with post-checks and a
deterministic fallback.

### Weeks 7–8 — Evaluation, hardening, second city

| Who | Work |
|---|---|
| B1 | Observability, trace ids, replay-based debugging |
| B2 | Multi-city isolation, data-freshness SLAs, load and latency |
| F | Accessibility, mobile, localisation (Greek/English) |
| J | **Owns the eval suite.** Runs it on every PR, reports the table. |
| Me | Incident runbook, on-call rotation, release criteria |

**Milestone:** second city added without code changes, only data. Eval table
published per release.

### Dependencies and review

The critical path is catalog → hours → travel → planner → validator → router →
narration. The LLM layer cannot start before the validator is trustworthy, which
is why it sits in weeks 5–6 and not week 2.

Every PR needs one reviewer and green CI. I review everything that touches the
validator, the router or a prompt. J's work is reviewed by me for four weeks,
then by B2, then normally — an explicit ramp, communicated as such so it does not
read as distrust.

---

## 3. Coaching exercise

*Scenario: a junior developer sends the user request and a list of attractions to
the LLM and asks it to produce a schedule. It looks convincing. Travel times are
sometimes impossible, museums may be closed, the route zig-zags, activities
overlap, weather is ignored.*

---

I would not open with the list of what is broken. I would open by asking them to
run their own thing twenty times with me, and we write down every output that is
wrong. Not "bad" — wrong. Impossible walk. Closed museum. Overlapping slots.

By output eight or nine they see the pattern themselves, and it is a better
pattern than "the prompt needs work". The failures are all arithmetic and
geometry. None of them are language failures. The prose is excellent every single
time.

That is the whole lesson, and I want them to reach it before I say it: **the model
is genuinely good at what it was asked to do, and we asked it to do the wrong
job.** Nothing about their work was stupid. It was a reasonable first attempt that
ran into a real property of language models, which is that they are fluent about
numbers without being reliable about them.

Then I would reframe rather than correct. The question is not "how do I make the
model schedule better". It is "which parts of this are the model's job at all".
Understanding what the user wants: model. Explaining why a neighbourhood is worth
seeing: model. Adding forty minutes to half past two, deciding whether a door is
open, knowing that two places are on opposite sides of the city: not the model.
Those have exact answers and code gives exact answers.

Their first task would not be a planner. It would be a **validator** — a function
that takes an itinerary and returns a list of specific violations with stable
codes. Overlap. Closed during visit. Travel gap too short. Nothing else. It is
small, it is fully specified, and it is checkable by hand.

I would ask them to run their existing prompt-generated plans through it. The
twenty outputs we counted together now get machine-readable reasons, and they
have built the thing that produced them. That is a much better moment than being
told.

After that we peel the solving into code one piece at a time, and each piece is
justified by a violation the validator keeps reporting. Opening hours first,
because that is the one that embarrasses you in front of a user. Then travel
times. Then ordering. The model keeps narration for the whole journey, and I
would be explicit that narration is not the consolation prize — it is the part
users actually experience, and it is the part the model is genuinely better at
than we are.

One thing I would say out loud, because juniors hear criticism louder than it is
meant: this is not a mistake anyone should feel bad about. Half the industry
shipped this exact architecture. The difference between a junior and a senior
here is not knowing the answer in advance. It is counting the failures instead of
trusting the demo.

*(490 words)*