from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.domain.catalog import PoiCatalog
from app.domain.models import GeoPoint
from app.domain.ports import TravelLeg, TravelMatrixRequest, TravelMatrixResult
from app.tools.catalog import PROJECT_ROOT, CatalogRepository

ATHENS = ZoneInfo("Europe/Athens")
MATRIX_PATH = PROJECT_ROOT / "data" / "walking_matrix.json"
DISTANCE_MULTIPLIER = 1.3
WALKING_SPEED_KMH = 4.5
CITY_BOUNDS = {"lat_min": 40.50, "lat_max": 40.75, "lon_min": 22.80, "lon_max": 23.10}


class MatrixFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: datetime
    mode: str = "foot-walking"
    source: str
    approximate: bool
    assumptions: dict[str, float]
    poi_ids: list[str]
    durations_minutes: dict[str, dict[str, int]]


def haversine_km(origin: GeoPoint, destination: GeoPoint) -> float:
    radius_km = 6371.0088
    lat1 = math.radians(origin.latitude)
    lat2 = math.radians(destination.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(destination.longitude - origin.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius_km * 2 * math.asin(math.sqrt(value))


def approximate_walking_minutes(origin: GeoPoint, destination: GeoPoint) -> int:
    if origin == destination:
        return 0
    route_km = haversine_km(origin, destination) * DISTANCE_MULTIPLIER
    return max(1, math.ceil(route_km / WALKING_SPEED_KMH * 60))


def build_matrix(catalog: PoiCatalog, ors_api_key: str | None = None) -> MatrixFile:
    ids = [poi.id for poi in catalog.pois]
    points = [poi.coordinates for poi in catalog.pois]
    for point in points:
        validate_city_point(point)

    if ors_api_key:
        durations = _ors_durations(points, ors_api_key)
        source = "openrouteservice"
        approximate = False
    else:
        durations = [
            [approximate_walking_minutes(origin, destination) for destination in points]
            for origin in points
        ]
        source = "haversine_fallback"
        approximate = True

    return MatrixFile(
        generated_at=datetime.now(ATHENS),
        source=source,
        approximate=approximate,
        assumptions={
            "distance_multiplier": DISTANCE_MULTIPLIER,
            "walking_speed_kmh": WALKING_SPEED_KMH,
        },
        poi_ids=ids,
        durations_minutes={
            origin_id: {
                destination_id: durations[origin_index][destination_index]
                for destination_index, destination_id in enumerate(ids)
            }
            for origin_index, origin_id in enumerate(ids)
        },
    )


def validate_city_point(point: GeoPoint) -> None:
    if not (
        CITY_BOUNDS["lat_min"] <= point.latitude <= CITY_BOUNDS["lat_max"]
        and CITY_BOUNDS["lon_min"] <= point.longitude <= CITY_BOUNDS["lon_max"]
    ):
        raise ValueError("coordinates are outside the Thessaloniki tool boundary")


def _ors_durations(points: list[GeoPoint], api_key: str) -> list[list[int]]:
    response = httpx.post(
        "https://api.openrouteservice.org/v2/matrix/foot-walking",
        headers={"Authorization": api_key},
        json={
            "locations": [[point.longitude, point.latitude] for point in points],
            "metrics": ["duration"],
        },
        timeout=30.0,
    )
    response.raise_for_status()
    raw = response.json()["durations"]
    return [[math.ceil(seconds / 60) for seconds in row] for row in raw]


def write_matrix(path: Path = MATRIX_PATH, ors_api_key: str | None = None) -> MatrixFile:
    repository = CatalogRepository()
    matrix = build_matrix(repository.catalog, ors_api_key)
    path.write_text(matrix.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return matrix


class PrecomputedTravelTimeProvider:
    def __init__(self, path: Path = MATRIX_PATH) -> None:
        self.matrix_file = MatrixFile.model_validate_json(path.read_text(encoding="utf-8"))

    def matrix(self, request: TravelMatrixRequest) -> TravelMatrixResult:
        for point in request.locations.values():
            validate_city_point(point)
        unknown = request.locations.keys() - self.matrix_file.durations_minutes.keys()
        if unknown:
            raise ValueError(f"locations missing from walking matrix: {sorted(unknown)}")

        legs = [
            TravelLeg(
                origin_id=origin_id,
                destination_id=destination_id,
                minutes=self.matrix_file.durations_minutes[origin_id][destination_id],
                approximate=self.matrix_file.approximate and origin_id != destination_id,
            )
            for origin_id in request.locations
            for destination_id in request.locations
        ]
        return TravelMatrixResult(legs=legs, source=self.matrix_file.source)


if __name__ == "__main__":
    settings = get_settings()
    result = write_matrix(ors_api_key=settings.ors_api_key or None)
    print(f"wrote {len(result.poi_ids)}x{len(result.poi_ids)} matrix from {result.source}")
