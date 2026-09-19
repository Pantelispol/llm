from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from opening_hours import OpeningHours, State

from app.domain.catalog import Poi, SeasonalHours
from app.domain.ports import OpeningHoursRequest, OpeningHoursResult, OpenInterval
from app.tools.catalog import CatalogRepository

ATHENS = ZoneInfo("Europe/Athens")


@dataclass(frozen=True)
class ResolvedDay:
    expression: str
    source_rule: str
    reason: str
    needs_verification: bool
    admission_eur: float | None = None


class OpeningHoursEngine:
    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def check(self, request: OpeningHoursRequest) -> OpeningHoursResult:
        poi = self.repository.get(request.poi_id)
        visit_start = request.visit_start.astimezone(ATHENS)
        visit_end = request.visit_end.astimezone(ATHENS)

        temporary_reason = self._temporary_closure_for_visit(poi, visit_start, visit_end)
        if temporary_reason:
            return OpeningHoursResult(
                can_visit=False,
                reason=f"closed: {temporary_reason}",
                source_rule="temporary_closure",
                needs_verification=poi.needs_verification.get("temporary_closures", True),
            )

        resolved = self._resolve_day(poi, visit_start.date())
        if resolved is None:
            return OpeningHoursResult(
                can_visit=False,
                reason="opening hours unknown for this date",
                source_rule=None,
                needs_verification=True,
            )
        if resolved.expression == "off":
            return OpeningHoursResult(
                can_visit=False,
                reason=f"closed: {resolved.reason}",
                source_rule=resolved.source_rule,
                admission_eur=resolved.admission_eur,
                needs_verification=resolved.needs_verification,
            )

        interval = self._open_interval_at(resolved.expression, visit_start)
        if interval is None:
            return OpeningHoursResult(
                can_visit=False,
                reason="closed at requested start time",
                source_rule=resolved.source_rule,
                admission_eur=resolved.admission_eur,
                needs_verification=resolved.needs_verification,
            )

        opens_at, closes_at = interval
        offset = poi.last_entry_before_close_min or 0
        last_entry_at = closes_at - timedelta(minutes=offset)
        if visit_start > last_entry_at:
            return OpeningHoursResult(
                can_visit=False,
                reason=f"last entry is {last_entry_at:%H:%M}",
                opens_at=opens_at,
                closes_at=closes_at,
                last_entry_at=last_entry_at,
                source_rule=resolved.source_rule,
                admission_eur=resolved.admission_eur,
                needs_verification=resolved.needs_verification,
            )

        if visit_end > closes_at:
            return OpeningHoursResult(
                can_visit=False,
                reason=f"visit would end after closing at {closes_at:%H:%M}",
                opens_at=opens_at,
                closes_at=closes_at,
                last_entry_at=last_entry_at,
                source_rule=resolved.source_rule,
                admission_eur=resolved.admission_eur,
                needs_verification=resolved.needs_verification,
            )

        return OpeningHoursResult(
            can_visit=True,
            reason="visit fits within opening hours and last entry",
            opens_at=opens_at,
            closes_at=closes_at,
            last_entry_at=last_entry_at,
            source_rule=resolved.source_rule,
            admission_eur=resolved.admission_eur,
            needs_verification=resolved.needs_verification,
        )

    def next_open_interval(
        self, poi_id: str, after: datetime, *, search_days: int = 370
    ) -> OpenInterval | None:
        poi = self.repository.get(poi_id)
        local_after = after.astimezone(ATHENS)
        for day_offset in range(search_days + 1):
            day = local_after.date() + timedelta(days=day_offset)
            if self._temporary_closure_on(poi, day):
                continue
            resolved = self._resolve_day(poi, day)
            if resolved is None or resolved.expression == "off":
                continue
            for opens_at, closes_at in self._open_intervals(resolved.expression, day):
                if closes_at <= local_after:
                    continue
                return OpenInterval(
                    opens_at=opens_at,
                    closes_at=closes_at,
                    last_entry_at=closes_at
                    - timedelta(minutes=poi.last_entry_before_close_min or 0),
                    source_rule=resolved.source_rule,
                    needs_verification=resolved.needs_verification,
                )
        return None

    def _resolve_day(self, poi: Poi, day: date) -> ResolvedDay | None:
        for override in poi.date_overrides:
            if override.date == day:
                return ResolvedDay(
                    expression=override.expression,
                    source_rule="date_override",
                    reason=override.reason,
                    admission_eur=override.admission_eur,
                    needs_verification=poi.needs_verification.get("date_overrides", True),
                )

        holiday_id = self.repository.holiday_id_on(day)
        month_day = day.strftime("%m-%d")
        for closure in poi.closed:
            if closure.annual_date == month_day or (
                closure.holiday_id is not None and closure.holiday_id == holiday_id
            ):
                return ResolvedDay(
                    expression="off",
                    source_rule="holiday",
                    reason=closure.reason,
                    needs_verification=poi.needs_verification.get("closed", True),
                )

        season = next(
            (period for period in poi.opening_hours if self._contains(period, month_day)),
            None,
        )
        if season is None:
            return None
        return ResolvedDay(
            expression=season.expression,
            source_rule="seasonal",
            reason="seasonal schedule",
            needs_verification=poi.needs_verification.get("opening_hours", True),
        )

    @staticmethod
    def _contains(period: SeasonalHours, month_day: str) -> bool:
        if period.valid_from <= period.valid_to:
            return period.valid_from <= month_day <= period.valid_to
        return month_day >= period.valid_from or month_day <= period.valid_to

    @staticmethod
    def _temporary_closure_on(poi: Poi, day: date) -> str | None:
        for closure in poi.temporary_closures:
            if closure.start <= day <= closure.end:
                return closure.reason
        return None

    def _temporary_closure_for_visit(
        self, poi: Poi, visit_start: datetime, visit_end: datetime
    ) -> str | None:
        final_moment = visit_end - timedelta(microseconds=1)
        day = visit_start.date()
        while day <= final_moment.date():
            if reason := self._temporary_closure_on(poi, day):
                return reason
            day += timedelta(days=1)
        return None

    def _open_interval_at(
        self, expression: str, visit_start: datetime
    ) -> tuple[datetime, datetime] | None:
        for opens_at, closes_at in self._open_intervals(expression, visit_start.date(), days=2):
            if opens_at <= visit_start < closes_at:
                return opens_at, closes_at
        return None

    @staticmethod
    def _open_intervals(
        expression: str, day: date, *, days: int = 1
    ) -> list[tuple[datetime, datetime]]:
        start = datetime.combine(day, time.min, tzinfo=ATHENS)
        end = datetime.combine(day + timedelta(days=days), time.min, tzinfo=ATHENS)
        parsed = OpeningHours(expression, timezone=ATHENS)
        return [
            (interval_start, interval_end)
            for interval_start, interval_end, state, _comment in parsed.intervals(start, end)
            if state == State.OPEN
        ]
