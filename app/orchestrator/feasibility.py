"""The feasibility stage: test the traveler's hypothesis, do not plan a new day.

A feasibility question ("can I visit X if I arrive at 16:45?") is a read-only
query. It names its own inputs (POIs, a day, an arrival time or a range) and
gets the exhaustive `FeasibilityChecker` verdict for exactly those, never a
different schedule the beam planner preferred. Nothing is committed and the
answer is rendered from the typed result, so there is no model in the answer
path to say "no" while the state says otherwise.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.domain.catalog import Poi, PoiCatalog
from app.domain.models import (
    AnswerLanguage,
    Intent,
    TripState,
    TurnAnalysis,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from app.domain.ports import OpeningHoursChecker
from app.planning.feasibility import (
    FeasibilityEstimate,
    FeasibilityFix,
    FeasibilityFixKind,
    FeasibilityResult,
    FeasibilityVerdict,
)

ATHENS = ZoneInfo("Europe/Athens")
PLAN_CHANGING_INTENTS = frozenset({Intent.CREATE_PLAN, Intent.EDIT_PLAN})

NEEDS_INPUT_ANSWER = {
    AnswerLanguage.EN: (
        "I need to know which place and roughly when you would arrive, for example "
        '"Can I visit the White Tower if I arrive at 16:45?", before I can check it.'
    ),
    AnswerLanguage.EL: (
        "Για να το ελέγξω, πείτε μου ποιο μέρος θέλετε να επισκεφθείτε και περίπου τι ώρα "
        "υπολογίζετε να φτάσετε, π.χ. «Προλαβαίνω τον Λευκό Πύργο αν φτάσω στις 16:45;»."
    ),
}


def is_feasibility_only(analysis: TurnAnalysis) -> bool:
    """True when the turn asks whether something fits and asks for no plan change."""
    return (
        Intent.FEASIBILITY_CHECK in analysis.intents
        and not PLAN_CHANGING_INTENTS.intersection(analysis.intents)
        and not analysis.plan_edits
    )


def hypothesis_poi_ids(analysis: TurnAnalysis, state: TripState, known: set[str]) -> list[str]:
    """Mentioned POIs; the stored plan's stops only when the turn names none."""
    mentioned = [p for p in analysis.entities.mentioned_poi_ids if p in known]
    if mentioned:
        return list(dict.fromkeys(mentioned))
    if state.itinerary is None:
        return []
    return list(dict.fromkeys(a.poi_id for a in state.itinerary.activities if a.poi_id in known))


def feasibility_violations(result: FeasibilityResult, catalog: PoiCatalog) -> list[Violation]:
    """Typed findings for the hypothesis, so callers need not parse the prose."""
    if result.verdict == FeasibilityVerdict.FEASIBLE:
        return list(result.notices)
    names = {poi.id: poi.names.en for poi in catalog.pois}
    found = list(result.notices)
    estimate = (
        result.comfortable if result.verdict != FeasibilityVerdict.TIGHT else result.lower_bound
    )
    seen: set[tuple[ViolationCode, str | None]] = {(v.code, v.poi_id) for v in found}

    def add(violation: Violation) -> None:
        key = (violation.code, violation.poi_id)
        if key not in seen:
            seen.add(key)
            found.append(violation)

    for stop in estimate.stops:
        for code in stop.issue_codes:
            add(
                Violation(
                    code=code,
                    message=(
                        f"{names.get(stop.poi_id, stop.poi_id)} cannot be visited in this window."
                    ),
                    poi_id=stop.poi_id,
                    activity_position=stop.position,
                )
            )
    for poi_id in result.comfortable.closed_poi_ids:
        add(
            Violation(
                code=ViolationCode.CLOSED_DURING_VISIT,
                message=(
                    f"{names.get(poi_id, poi_id)} has no open slot that fits on the requested day."
                ),
                poi_id=poi_id,
            )
        )
    if result.verdict == FeasibilityVerdict.TIGHT:
        add(
            Violation(
                code=ViolationCode.OUTSIDE_USER_WINDOW,
                severity=ViolationSeverity.WARNING,
                message="Fits only at minimum visit durations, not at comfortable ones.",
            )
        )
    elif not any(v.severity == ViolationSeverity.ERROR for v in found):
        add(
            Violation(
                code=ViolationCode.OUTSIDE_USER_WINDOW,
                message="The visits do not fit inside the requested window.",
            )
        )
    return found


def _fmt(value: datetime | None) -> str:
    return "?" if value is None else value.astimezone(ATHENS).strftime("%H:%M")


def _name(poi: Poi | None, poi_id: str, language: AnswerLanguage) -> str:
    if poi is None:
        return poi_id
    return poi.names.el if language == AnswerLanguage.EL else poi.names.en


def _closed_reason(
    poi_id: str,
    arrival: datetime,
    hours: OpeningHoursChecker,
    name: str,
    language: AnswerLanguage,
) -> str:
    day_start = datetime.combine(arrival.astimezone(ATHENS).date(), time(0, 1), tzinfo=ATHENS)
    interval = hours.next_open_interval(poi_id, day_start)
    same_day = (
        interval is not None and interval.opens_at.astimezone(ATHENS).date() == day_start.date()
    )
    if not same_day or interval is None:
        return (
            f"{name} is closed that day."
            if language == AnswerLanguage.EN
            else f"{name}: δεν δέχεται επισκέψεις εκείνη την ημέρα."
        )
    if language == AnswerLanguage.EL:
        return (
            f"{name}: η τελευταία είσοδος είναι στις {_fmt(interval.last_entry_at)} και "
            f"κλείνει στις {_fmt(interval.closes_at)}, ενώ θα φτάνατε στις {_fmt(arrival)}."
        )
    return (
        f"{name} admits its last visitors at {_fmt(interval.last_entry_at)} and closes at "
        f"{_fmt(interval.closes_at)}; you would arrive at {_fmt(arrival)}."
    )


def _fix_text(fix: FeasibilityFix, poi: Poi | None, language: AnswerLanguage) -> str:
    name = _name(poi, fix.poi_id or "", language)
    if language == AnswerLanguage.EN:
        return fix.message
    match fix.kind:
        case FeasibilityFixKind.DROP_POI:
            return f"αν αφαιρέσετε τη στάση «{name}», το υπόλοιπο πρόγραμμα χωράει με άνεση."
        case FeasibilityFixKind.START_EARLIER:
            return f"αν ξεκινήσετε {fix.minutes} λεπτά νωρίτερα, χωρούν όλες οι στάσεις."
        case FeasibilityFixKind.VISIT_ANOTHER_DAY:
            return f"προγραμματίστε τη στάση «{name}» για κάποια άλλη ημέρα."
        case _:
            return (
                "δεν έχω διαθέσιμους χρόνους μετακίνησης με ταξί ή μέσα μαζικής μεταφοράς, "
                "οπότε δεν μπορώ να υπολογίσω μια ταχύτερη εναλλακτική."
            )


def _schedule(estimate: FeasibilityEstimate, pois: dict[str, Poi], language: AnswerLanguage) -> str:
    return "; ".join(
        f"{_name(pois.get(stop.poi_id), stop.poi_id, language)} "
        f"{_fmt(stop.visit_start)}–{_fmt(stop.departure)}"
        for stop in estimate.stops
        if stop.visit_start is not None
    )


def _notice_text(notice: Violation, pois: dict[str, Poi], language: AnswerLanguage) -> str:
    if language == AnswerLanguage.EN:
        return notice.message
    name = _name(pois.get(notice.poi_id or ""), notice.poi_id or "", language)
    if notice.code == ViolationCode.UNKNOWN_POI:
        return f"Δεν βρήκα το μέρος «{name}» στον κατάλογο."
    if notice.code == ViolationCode.EXCLUDED_CATEGORY:
        return f"{name}: ανήκει σε κατηγορία που έχετε εξαιρέσει."
    return f"{name}: έχετε εξαιρέσει αυτή τη στάση από το πρόγραμμά σας."


def render_feasibility_answer(
    result: FeasibilityResult,
    *,
    catalog: PoiCatalog,
    hours: OpeningHoursChecker,
    language: AnswerLanguage,
    window_end: datetime,
) -> str:
    pois = {poi.id: poi for poi in catalog.pois}
    el = language == AnswerLanguage.EL
    lines: list[str] = []
    best = result.comfortable

    if result.verdict == FeasibilityVerdict.FEASIBLE:
        lines.append(
            "Ναι, προλαβαίνετε μέσα στον διαθέσιμο χρόνο."
            if el
            else "Yes, that fits."
        )
        lines.append(
            ("Προτεινόμενη σειρά επισκέψεων: " if el else "Suggested order: ")
            + _schedule(best, pois, language)
            + "."
        )
        if best.margin_to_deadline_minutes is not None:
            margin = best.margin_to_deadline_minutes
            lines.append(
                f"Θα ολοκληρώσετε στις {_fmt(best.finish_at)}, {margin} λεπτά πριν από "
                f"τις {_fmt(window_end)}."
                if el
                else (
                    f"You would finish at {_fmt(best.finish_at)}, "
                    f"{margin} min before {_fmt(window_end)}."
                )
            )
    elif result.verdict == FeasibilityVerdict.TIGHT:
        lines.append(
            "Είναι οριακά εφικτό: χωράει μόνο αν περιορίσετε τις επισκέψεις στις ελάχιστες "
            "διάρκειές τους."
            if el
            else "Only just: it fits at minimum visit lengths but not at comfortable ones."
        )
        lines.append(
            ("Πρόγραμμα με ελάχιστες διάρκειες: " if el else "Minimum schedule: ")
            + _schedule(result.lower_bound, pois, language)
            + "."
        )
    else:
        lines.append("Όχι, δεν προλαβαίνετε." if el else "No, that does not fit.")
        for stop in best.stops:
            if (
                stop.poi_id in best.closed_poi_ids
                or ViolationCode.HOURS_UNKNOWN_FOR_DATE in stop.issue_codes
            ):
                lines.append(
                    _closed_reason(
                        stop.poi_id,
                        stop.arrival,
                        hours,
                        _name(pois.get(stop.poi_id), stop.poi_id, language),
                        language,
                    )
                )
        if not best.closed_poi_ids and best.margin_to_deadline_minutes is not None:
            over = -best.margin_to_deadline_minutes
            if over > 0:
                lines.append(
                    f"Με άνετο ρυθμό θα τελειώνατε {over} λεπτά μετά τις {_fmt(window_end)}."
                    if el
                    else (
                        f"At comfortable pacing you would finish {over} min "
                        f"after {_fmt(window_end)}."
                    )
                )
        for notice in result.notices:
            if notice.code in {
                ViolationCode.EXCLUDED_POI,
                ViolationCode.EXCLUDED_CATEGORY,
                ViolationCode.UNKNOWN_POI,
            }:
                lines.append(_notice_text(notice, pois, language))

    for fix in result.minimal_fixes:
        text = _fix_text(fix, pois.get(fix.poi_id or ""), language)
        # Lower-cased so the verb after the label is not read as a proper name.
        label = "Option: "
        if el:
            label = (
                "Σημείωση: "
                if fix.kind == FeasibilityFixKind.TRAVEL_TIMES_UNAVAILABLE
                else "Πρόταση: "
            )
        lines.append(label + text[0].lower() + text[1:])
    lines.append(
        "Το πρόγραμμά σας παραμένει αμετάβλητο." if el else "I have not changed your itinerary."
    )
    return " ".join(lines)
