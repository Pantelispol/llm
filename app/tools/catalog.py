from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from opening_hours import OpeningHours

from app.domain.catalog import HolidayCalendar, Poi, PoiCatalog

PROJECT_ROOT = Path(__file__).parents[2]


class CatalogRepository:
    def __init__(
        self,
        poi_path: Path = PROJECT_ROOT / "data" / "pois.yaml",
        holiday_path: Path = PROJECT_ROOT / "data" / "holidays.yaml",
    ) -> None:
        self.catalog = PoiCatalog.model_validate(self._read_yaml(poi_path))
        self.holidays = HolidayCalendar.model_validate(self._read_yaml(holiday_path))
        self._pois = {poi.id: poi for poi in self.catalog.pois}
        self._holidays_by_date = {holiday.date: holiday for holiday in self.holidays.holidays}
        self._validate_opening_expressions()

    def get(self, poi_id: str) -> Poi:
        try:
            return self._pois[poi_id]
        except KeyError as error:
            raise KeyError(f"unknown POI id: {poi_id}") from error

    def holiday_id_on(self, day: date) -> str | None:
        holiday = self._holidays_by_date.get(day)
        return holiday.id if holiday else None

    @staticmethod
    def _read_yaml(path: Path) -> dict:
        with path.open(encoding="utf-8") as file:
            value = yaml.safe_load(file)
        if not isinstance(value, dict):
            raise ValueError(f"expected a YAML object in {path}")
        return value

    def _validate_opening_expressions(self) -> None:
        for poi in self.catalog.pois:
            expressions = [period.expression for period in poi.opening_hours]
            expressions.extend(override.expression for override in poi.date_overrides)
            for expression in expressions:
                try:
                    OpeningHours(expression)
                except Exception as error:
                    raise ValueError(
                        f"invalid opening_hours expression for {poi.id}: {expression}"
                    ) from error
