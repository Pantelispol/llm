from pathlib import Path

import pytest

from app.domain.models import GeoPoint
from app.domain.ports import TravelMatrixRequest
from app.tools.catalog import CatalogRepository
from app.tools.travel import (
    MATRIX_PATH,
    MatrixFile,
    PrecomputedTravelTimeProvider,
    approximate_walking_minutes,
    build_matrix,
)

FIXED_MATRIX_PATH = Path(__file__).parent / "fixtures" / "walking_matrix_fixed.json"


def test_committed_matrix_covers_catalog_and_source_matches_approximation() -> None:
    repository = CatalogRepository()
    matrix = MatrixFile.model_validate_json(MATRIX_PATH.read_text(encoding="utf-8"))
    poi_ids = [poi.id for poi in repository.catalog.pois]

    assert matrix.poi_ids == poi_ids
    assert matrix.source in {"openrouteservice", "haversine_fallback"}
    assert matrix.approximate is (matrix.source == "haversine_fallback")
    assert set(matrix.durations_minutes) == set(poi_ids)
    assert all(set(row) == set(poi_ids) for row in matrix.durations_minutes.values())


def test_fallback_matrix_is_symmetric_with_zero_diagonal() -> None:
    matrix = build_matrix(CatalogRepository().catalog)
    for origin in matrix.poi_ids:
        assert matrix.durations_minutes[origin][origin] == 0
        for destination in matrix.poi_ids:
            assert (
                matrix.durations_minutes[origin][destination]
                == matrix.durations_minutes[destination][origin]
            )


def test_fallback_formula_is_explicit_and_rounds_up() -> None:
    origin = GeoPoint(latitude=40.6264, longitude=22.9484)
    destination = GeoPoint(latitude=40.6258, longitude=22.9540)
    assert approximate_walking_minutes(origin, destination) == 9


def test_precomputed_provider_returns_requested_subset() -> None:
    repository = CatalogRepository()
    coordinates = {poi.id: poi.coordinates for poi in repository.catalog.pois}
    provider = PrecomputedTravelTimeProvider(FIXED_MATRIX_PATH)
    result = provider.matrix(
        TravelMatrixRequest(
            locations={
                "white_tower": coordinates["white_tower"],
                "archaeological_museum": coordinates["archaeological_museum"],
            }
        )
    )

    assert len(result.legs) == 4
    assert result.source == "haversine_fallback"
    assert (
        next(
            leg
            for leg in result.legs
            if leg.origin_id == "white_tower" and leg.destination_id == "white_tower"
        ).approximate
        is False
    )
    assert (
        next(
            leg
            for leg in result.legs
            if leg.origin_id == "white_tower" and leg.destination_id == "archaeological_museum"
        ).approximate
        is True
    )


def test_tool_rejects_coordinates_outside_city_boundary() -> None:
    provider = PrecomputedTravelTimeProvider()
    with pytest.raises(ValueError, match="outside the Thessaloniki"):
        provider.matrix(
            TravelMatrixRequest(
                locations={
                    "white_tower": GeoPoint(latitude=40.6264, longitude=22.9484),
                    "archaeological_museum": GeoPoint(latitude=37.9838, longitude=23.7275),
                }
            )
        )


def test_builder_without_key_uses_fallback_without_network() -> None:
    matrix = build_matrix(CatalogRepository().catalog)
    assert matrix.source == "haversine_fallback"
    assert matrix.approximate is True


def test_matrix_file_is_in_data_directory() -> None:
    assert Path(__file__).parents[1] / "data" / "walking_matrix.json" == MATRIX_PATH
