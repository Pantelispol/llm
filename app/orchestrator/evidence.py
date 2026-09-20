"""Builds the per-call evidence registry from what tools actually returned.

Every id here is typed and traceable: RAG items keep their `chunk_id`, and
operational items carry a `catalog:`, `weather:`, or `travel:` id naming the
tool and field they came from. Narration may cite nothing else.

Operational statements are phrased as complete standalone clauses because the
`{{fact:...}}` token substitutes them verbatim. Fragments produced sentences
like "the first stop is open during the scheduled visit open from 08:00 to
20:00" in the narrate.v1 recordings; narrate.v2 phrases them to stand alone.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.models import EvidenceItem, EvidenceKind
from app.domain.ports import OpeningHoursChecker, RetrievalHit
from app.tools.catalog import CatalogRepository

ATHENS = ZoneInfo("Europe/Athens")
FULL_DAY = timedelta(hours=24)


def hours_evidence_id(poi_id: str) -> str:
    return f"catalog:{poi_id}:hours"


def admission_evidence_id(poi_id: str) -> str:
    return f"catalog:{poi_id}:admission"


def hours_evidence(
    hours: OpeningHoursChecker,
    poi_id: str,
    day: date,
) -> EvidenceItem | None:
    """One day's opening interval for a POI, as the hours engine resolved it."""
    interval = hours.next_open_interval(
        poi_id, datetime.combine(day, time(0, 1), tzinfo=ATHENS)
    )
    if interval is None:
        return None
    if interval.opens_at.date() != day:
        statement = (
            f"This place is closed on {day.isoformat()}; it next opens on "
            f"{interval.opens_at.date().isoformat()}"
        )
    elif interval.closes_at - interval.opens_at >= FULL_DAY:
        statement = f"This place is open at all hours on {day.isoformat()}"
    else:
        statement = (
            f"Opening hours on {day.isoformat()} are {interval.opens_at:%H:%M} to "
            f"{interval.closes_at:%H:%M}, with last entry at {interval.last_entry_at:%H:%M}"
        )
    return EvidenceItem(
        evidence_id=hours_evidence_id(poi_id),
        kind=EvidenceKind.HOURS,
        poi_id=poi_id,
        text=statement,
        source=f"catalog:{interval.source_rule}",
        continuously_open=(
            interval.opens_at.date() == day
            and interval.closes_at - interval.opens_at >= FULL_DAY
        ),
    )


def admission_evidence(
    repository: CatalogRepository,
    poi_id: str,
) -> EvidenceItem | None:
    """The catalog admission price, or nothing when the catalog records none."""
    poi = repository.get(poi_id)
    if poi.price_eur is None:
        return None
    statement = f"Standard admission is {poi.price_eur.standard:.2f} euro"
    if poi.price_eur.reduced is not None:
        statement += f", and the reduced ticket is {poi.price_eur.reduced:.2f} euro"
    return EvidenceItem(
        evidence_id=admission_evidence_id(poi_id),
        kind=EvidenceKind.CATALOG,
        poi_id=poi_id,
        text=statement,
        source="catalog:price_eur",
        admission_eur=poi.price_eur.standard,
    )


def weather_unavailable_evidence(day: date, reason: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"weather:{day.isoformat()}:unavailable",
        kind=EvidenceKind.WEATHER,
        text=(
            f"Weather data was unavailable for {day.isoformat()}, so no weather risk "
            f"could be cleared ({reason})"
        ),
        source="weather:open-meteo",
    )


def weather_summary_evidence(day: date, source: str, summary: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"weather:{day.isoformat()}:summary",
        kind=EvidenceKind.WEATHER,
        text=f"The forecast for {day.isoformat()} reports {summary}",
        source=f"weather:{source}",
    )


def approximate_travel_evidence(source: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="travel:matrix:approximate",
        kind=EvidenceKind.TRAVEL,
        text=(
            "Walking times are estimates from an approximate fallback matrix rather "
            "than measured routes"
        ),
        source=f"travel:{source}",
    )


def retrieval_evidence(hit: RetrievalHit) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=hit.chunk_id,
        kind=EvidenceKind.RAG,
        poi_id=hit.poi_id,
        text=hit.text,
        source=hit.source_url or f"data/content/{hit.poi_id}.md",
        is_untrusted=hit.is_untrusted,
    )
