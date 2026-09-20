# Production Incident Exercise

**User report:** *"The assistant told me to visit a museum at 18:00, but when I
arrived it had already closed."*

---

## Why this one is not hypothetical

This exact failure happened during the build, in the data pipeline rather than
in production. The first draft of the Archaeological Museum record used a
secondary tourism site giving seasonal hours of 08:00–20:00 in summer and
09:00–16:00 in winter. The official museum site gives **09:00–17:00 year-round**.

An itinerary built on that record would have sent someone to a museum at 18:00
in July, confidently, with a plausible explanation attached. Manual verification
caught it. No test would have, because the code was correct and the data was
wrong.

What follows is how I would investigate the production version, written against
what the system actually records.

---

## 1. Reproduce before theorising

The first move is not a hypothesis. It is a replay.

Every turn records a trace id, the resolved `now`, the `TripState`, the tool
calls made, the weather fixture or live response, the validated plan JSON, and
the model's raw narration draft alongside the delivered answer. The record/replay
harness replays a stored turn through the same parsers, the same validator and
the same post-checks, with no network calls.

So: find the trace, replay it, and see whether the failure reproduces. If it
reproduces, the bug is in code or data and is now in front of me. If it does not,
the bug is in something that changed since — a data refresh, a model update, a
live API response — and that difference is itself the finding.

## 2. Walk the pipeline in order, and stop at the first layer that is wrong

The layers, and the question at each:

**Source data.** Which opening-hours record did this turn use? What is its
`source.kind`, `verified_at`, and confidence? Does it have a temporary closure or
date override that should have applied? This is where the real incident lived,
and it is where I would look first, because it is both the most common cause and
the cheapest to check.

**Season and calendar.** Was the correct seasonal schedule selected for that
date? Was it a public holiday, and was the holiday rule applied per-POI rather
than globally? Greek national holidays do not close every attraction — the
Archaeological Museum is open with free entry on 28 October, and a global holiday
rule would have been wrong in the opposite direction.

**Timezone and DST.** Was `now` resolved in Europe/Athens, and did the turn cross
a DST boundary? A one-hour error at 17:00 produces exactly this complaint.

**The validator.** Did it run at all? Did it check the *whole visit* rather than
just the start time, and did it check last entry? A visit starting at 17:45 with
a 17:00 close should produce `CLOSED_DURING_VISIT`; one starting at 16:50 against
a 17:00 last entry should produce `AFTER_LAST_ENTRY`. If the plan passed
validation, either the hours record was wrong or the validator has a gap, and
those are different bugs.

**The router.** Did the turn route through the planning chain at all? A turn that
skipped the hours check would produce this. This is not theoretical — the eval
harness found a routing defect where any turn containing "tomorrow" was misrouted
into the planner, and the reverse failure is equally possible.

**Narration drift.** Did the delivered answer match the validated plan JSON? The
post-checks make a raw clock time in narration a rejection rather than an output,
and the time tokens render from the plan by construction. But I would still check
the stored draft against the stored plan, because "the checks would have caught
it" is a claim, and the log can settle it.

**The model.** Last, not first. If the plan JSON is correct and the delivered
text is correct and the arrival was still wrong, the model was never the cause.

## 3. Immediate mitigation

Within the hour, independent of root cause:

- Correct the hours record against the official source, set `verified_at`, and
  drop the confidence on any record sharing that source.
- Add a defensive rule: refuse to schedule a visit whose end falls within a
  short margin of close when the record's confidence is below a threshold, and
  say so in the answer rather than silently dropping the stop.
- Sweep the catalog for every record sharing the bad source and flag them all.
  One wrong record from a source means the source is suspect, not that one field
  was unlucky.
- Reply to the user with the corrected hours and an acknowledgement. Not a
  template apology.

## 4. Architectural prevention

The design already contains most of the answer, which is why the real incident
was caught before shipping:

- **Source precedence, explicit.** Temporary closure > date override > per-POI
  holiday > seasonal schedule, with official sources outranking listings.
- **Conservative on conflict.** When two sources disagree and neither is clearly
  authoritative, the field is recorded as **unknown** rather than resolved to the
  more convenient value. The Jewish Museum's Monday hours are stored this way.
  Unknown hours are a validator error, so the planner cannot schedule into them.
- **`verified_at` and confidence on every field**, with a review queue.

What the incident shows is missing:

- **A freshness SLA per source kind**, enforced rather than documented. An
  official-site record older than a quarter should degrade to low confidence
  automatically, and low-confidence hours should not carry a visit that ends
  near closing.
- **Automated cross-checking** against the official source on a schedule, with a
  diff for human review. The real incident was caught by a person. That does not
  scale to thousands of attractions across many cities.
- **A canary eval case per high-traffic POI**, asserting that a late-afternoon
  visit is refused when the venue closes.

## 5. The outcome that matters

The fix is not the fix. The regression case is.

Every incident of this class ends with a new case in `evals/cases.yaml` that
fails against the old data and passes against the new, so the same failure cannot
return silently. The suite already carries the last-entry edge case for this
museum for exactly that reason.

An incident that produces only a corrected row in a YAML file has taught the
system nothing.