"""Template narration used when the model cannot produce a grounded answer.

The template is written in the same controlled-token language as the model
draft and rendered by the same renderer, so it can only contain canonical
catalog names, exact validated-plan times, verbatim operational facts, and
citation ids from this call's registry. It never paraphrases retrieved history
and never invents a transition: a user may get plain output, but not a silently
wrong answer. Its fixed sentences are translated so a Greek turn does not fall
back into English.
"""

from __future__ import annotations

from app.domain.catalog import PoiCatalog
from app.domain.models import (
    OPERATIONAL_EVIDENCE_KINDS,
    ActivityKind,
    AnswerLanguage,
    EvidenceItem,
    NarrationBundle,
    NarrationDisclosure,
)
from app.llm.narration_tokens import RenderedAnswer, render_tokens

PLAN_HEADING = {
    AnswerLanguage.EN: "Here is the validated schedule for your visit to Thessaloniki.",
    AnswerLanguage.EL: (
        "Αυτό είναι το επιβεβαιωμένο πρόγραμμα για την επίσκεψή σας στη Θεσσαλονίκη."
    ),
}
FACTS_HEADING = {
    AnswerLanguage.EN: "Here are the operational facts on record for your question.",
    AnswerLanguage.EL: "Αυτά είναι τα καταγεγραμμένα στοιχεία για την ερώτησή σας.",
}
NO_ANSWER = {
    AnswerLanguage.EN: (
        "There is nothing on record that answers this question, so nothing is stated here."
    ),
    AnswerLanguage.EL: (
        "Δεν υπάρχει καταγεγραμμένο στοιχείο για αυτή την ερώτηση, οπότε δεν αναφέρεται τίποτα."
    ),
}
POI_STOP_LINE = {
    AnswerLanguage.EN: "{position}. {start} to {end} at {label}.{citations}",
    AnswerLanguage.EL: "{position}. {start} έως {end} στο {label}.{citations}",
}
OTHER_STOP_LINE = {
    AnswerLanguage.EN: "{position}. {start} to {end}: {label}.{citations}",
    AnswerLanguage.EL: "{position}. {start} έως {end}: {label}.{citations}",
}
#: A break or an area meal has no catalog id, so it is named by a neutral
#: localized word. The planner's own area label mentions a nearby POI, and a
#: raw place name outside a token is exactly what the post-check rejects.
ACTIVITY_LABEL = {
    AnswerLanguage.EN: {
        ActivityKind.BREAK: "a rest stop",
        ActivityKind.MEAL: "a meal stop",
        ActivityKind.VISIT: "a stop",
    },
    AnswerLanguage.EL: {
        ActivityKind.BREAK: "μια στάση ξεκούρασης",
        ActivityKind.MEAL: "μια στάση για φαγητό",
        ActivityKind.VISIT: "μια στάση",
    },
}
TOTAL_WALKING = {
    AnswerLanguage.EN: "Total walking time in this schedule: {minutes} minutes.",
    AnswerLanguage.EL: "Συνολικός χρόνος περπατήματος στο πρόγραμμα: {minutes} λεπτά.",
}
DISCLOSURE_SENTENCES = {
    AnswerLanguage.EN: {
        NarrationDisclosure.APPROXIMATE_TRAVEL: (
            "Walking times come from an approximate matrix, so treat them as estimates."
        ),
        NarrationDisclosure.UNVERIFIED_HOURS: (
            "Opening hours for at least one stop are not fully verified; confirm before you go."
        ),
        NarrationDisclosure.WEATHER_UNAVAILABLE: (
            "Weather data was unavailable for this window, so no weather risk was cleared."
        ),
        NarrationDisclosure.DRAFT_CONTENT: (
            "Background descriptions are draft content pending human review."
        ),
    },
    AnswerLanguage.EL: {
        NarrationDisclosure.APPROXIMATE_TRAVEL: (
            "Οι χρόνοι περπατήματος είναι κατά προσέγγιση και όχι μετρημένες διαδρομές."
        ),
        NarrationDisclosure.UNVERIFIED_HOURS: (
            "Το ωράριο μίας τουλάχιστον στάσης δεν είναι πλήρως επιβεβαιωμένο· "
            "επιβεβαιώστε το πριν πάτε."
        ),
        NarrationDisclosure.WEATHER_UNAVAILABLE: (
            "Δεν υπήρχαν δεδομένα καιρού για αυτό το διάστημα, οπότε δεν ελέγχθηκε "
            "κίνδυνος από τον καιρό."
        ),
        NarrationDisclosure.DRAFT_CONTENT: (
            "Οι περιγραφές υποβάθρου είναι προσχέδιο και εκκρεμεί ανθρώπινος έλεγχος."
        ),
    },
}


def _operational_evidence(bundle: NarrationBundle, poi_id: str | None) -> list[EvidenceItem]:
    return [
        item
        for item in bundle.evidence
        if item.kind in OPERATIONAL_EVIDENCE_KINDS and item.poi_id == poi_id
    ]


def build_template(bundle: NarrationBundle) -> str:
    """Build the fallback answer in controlled-token form, before rendering."""
    language = bundle.language
    lines: list[str] = []
    if bundle.plan is not None and bundle.plan.activities:
        lines.append(PLAN_HEADING[language])
        lines.append("")
        for position, activity in enumerate(bundle.plan.activities, start=1):
            if activity.poi_id:
                template = POI_STOP_LINE[language]
                label = f"{{{{poi:{activity.poi_id}}}}}"
            else:
                template = OTHER_STOP_LINE[language]
                label = ACTIVITY_LABEL[language][activity.kind]
            citations = "".join(
                f" {{{{cite:{item.evidence_id}}}}}"
                for item in _operational_evidence(bundle, activity.poi_id)
            )
            lines.append(
                template.format(
                    position=position,
                    start=f"{{{{time:{position}:start}}}}",
                    end=f"{{{{time:{position}:end}}}}",
                    label=label,
                    citations=citations,
                )
            )
        lines.append("")
        lines.append(
            TOTAL_WALKING[language].format(minutes=bundle.plan.total_travel_minutes)
        )
    else:
        operational = [
            item for item in bundle.evidence if item.kind in OPERATIONAL_EVIDENCE_KINDS
        ]
        if operational:
            lines.append(FACTS_HEADING[language])
            lines.append("")
            for item in operational:
                subject = f"{{{{poi:{item.poi_id}}}}} — " if item.poi_id else ""
                lines.append(
                    f"- {subject}{{{{fact:{item.evidence_id}}}}} "
                    f"{{{{cite:{item.evidence_id}}}}}"
                )
        else:
            lines.append(NO_ANSWER[language])

    disclosures = [
        DISCLOSURE_SENTENCES[language][disclosure]
        for disclosure in bundle.disclosures
        if disclosure in DISCLOSURE_SENTENCES[language]
    ]
    if disclosures:
        lines.append("")
        lines.extend(disclosures)
    return "\n".join(lines).strip()


def narrate_deterministically(bundle: NarrationBundle, catalog: PoiCatalog) -> RenderedAnswer:
    return render_tokens(build_template(bundle), bundle, catalog)
