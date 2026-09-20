Phase 4c: plan quality. Phase 4 produces VALID but POOR plans.
Evidence from the demo CLI (before the hours update):
- five_hours_history ends 12:38 in a 5h window; AMTh/Byzantine/Ladadika
  dropped as NOT_ENOUGH_TIME although time was available.
- aristotelous_square appears as a 30-min "visit" only because it is the
  default start location.
- Lunch at 11:53; meal chosen uphill in Ano Poli.
- rain_after_16 removed white_tower "for weather" although it is an indoor
  museum visited before 16:00, and added nothing indoor after 16:00 while
  the Byzantine museum is open until 20:00.
- with_child picks 21-24 min uphill walks despite LONG_WALK_WITH_CHILD
  warnings; flat waterfront/parks are dropped as LOW_INTEREST; a break is
  placed right after a meal.

Fixes:
1. Data classification: ano_poli_walls and arch_of_galerius are outdoor
   public spaces -> opening_hours "24/7" (kind public_space). Audit all
   POIs for the same mistake and list changes. Do not invent hours for
   anything else.
2. Start location is a location, not an activity. Include a POI at the
   start only if it is selected on merit.
3. Objective: add a utilization term so plans use the window (target
   >= 80% of the window when enough open candidates exist). Re-check the
   travel weight: it must not dominate visit value.
4. Honest drop reasons: NOT_ENOUGH_TIME only if inserting the candidate
   anywhere would violate the window. CLOSED when the hours engine says so
   for the whole window. Otherwise LOWER_SCORE with the score gap vs the
   weakest selected activity. Test this.
5. Meals: lunch window 13:00-15:30 (dinner 19:30-21:30 if applicable).
   Meal areas: kapani_market, modiano_market, ladadika (check they are
   open at meal time via the hours engine), plus tsinari_ano_poli only
   when the route is already in Ano Poli. Prefer the nearest open, flat,
   central area. No break within 45 min after a meal.
6. Weather repair order: (a) reorder so outdoor happens before the risk
   window, (b) swap outdoor activities inside the risk window for indoor
   ones, (c) fill freed time with indoor candidates open during the risk
   window, (d) remove only as last resort. Never remove an indoor activity
   or one outside the risk window for weather. Check white_tower exposure
   in pois.yaml.
7. Child: stronger walking penalty (hard cap 25 min per leg, soft penalty
   from 15), a child_suitability bonus so parks/waterfront compete with
   interest match, and prefer flat routes (hilly POIs penalized).
8. Quality assertions (new test file, reusable in evals later):
   - window utilization >= 80% when candidates allow it
   - history plan includes at least two open core history POIs
     (rotunda, hagios_demetrios, hagia_sophia, acheiropoietos,
     arch_of_galerius, roman_forum, archaeological_museum,
     museum_of_byzantine_culture, white_tower)
   - roman_forum is never scheduled on Tuesday 2026-09-22 and its drop
     reason is CLOSED
   - rain_after_16: no outdoor after 16:00 AND at least one indoor
     activity after 16:00
   - lunch inside its window; no break within 45 min after a meal
   - with child: no leg > 25 min
   - drop reasons consistent with rule 4
9. Re-run all 7 demo scenarios and paste the FULL timetables, diffs and
   drop reasons in your summary (do not collapse them).

decisions-log.md: "feasible is not good": why quality assertions exist,
utilization term, weather repair order, honest drop reasons.

Commit as "Phase 4c: plan quality". Stop.