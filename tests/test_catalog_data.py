from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]

EXPECTED_POI_IDS = {
    "white_tower",
    "rotunda",
    "arch_of_galerius",
    "roman_forum",
    "hagios_demetrios",
    "acheiropoietos",
    "hagia_sophia",
    "heptapyrgio",
    "ano_poli_walls",
    "archaeological_museum",
    "museum_of_byzantine_culture",
    "jewish_museum",
    "bey_hamam",
    "aristotelous_square",
    "ladadika",
    "kapani_market",
    "modiano_market",
    "new_waterfront_umbrellas",
    "nea_paralia_parks",
    "seich_sou_forest",
    "tsinari_ano_poli",
    "vlatadon_monastery",
}

BASE_VERIFICATION_FIELDS = {
    "names.en",
    "names.el",
    "aliases",
    "coordinates",
    "category",
    "tags",
    "exposure",
    "visit_minutes",
    "opening_hours",
    "last_entry_offset_minutes",
    "child_friendly",
    "step_free",
    "heat_exposure",
    "price",
    "safety_tier",
}


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "data" / name).read_text())


def by_id() -> dict[str, dict]:
    return {poi["id"]: poi for poi in load_yaml("pois.yaml")["pois"]}


def test_catalog_has_exact_requested_pois_and_verification_flags() -> None:
    pois = by_id()
    assert set(pois) == EXPECTED_POI_IDS
    assert len(pois) == 22

    for poi in pois.values():
        assert poi["needs_verification"].keys() >= BASE_VERIFICATION_FIELDS
        assert all(isinstance(value, bool) for value in poi["needs_verification"].values())


def test_coordinates_are_rounded_and_all_require_map_verification() -> None:
    for poi in by_id().values():
        coordinates = poi["coordinates"]
        assert coordinates["latitude"] == round(coordinates["latitude"], 4)
        assert coordinates["longitude"] == round(coordinates["longitude"], 4)
        assert poi["needs_verification"]["coordinates"] is True


def test_archaeological_museum_keeps_supplied_seasons_and_conflict() -> None:
    museum = by_id()["archaeological_museum"]
    assert museum["opening_hours"] == [
        {
            "valid_from": "04-15",
            "valid_to": "11-14",
            "expression": "Mo-Su 08:00-20:00",
        },
        {
            "valid_from": "11-15",
            "valid_to": "04-14",
            "expression": "Mo-Su 09:00-16:00",
        },
    ]
    assert museum["last_entry_offset_minutes"] == 30
    assert museum["needs_verification"]["opening_hours"] is False
    assert museum["conflicts"][0]["source"]["kind"] == "third_party"


def test_required_public_spaces_have_explicit_24_7_periods() -> None:
    expected = {
        "aristotelous_square",
        "ladadika",
        "new_waterfront_umbrellas",
        "nea_paralia_parks",
        "tsinari_ano_poli",
    }
    pois = by_id()
    for poi_id in expected:
        assert pois[poi_id]["opening_hours"][0]["expression"] == "24/7"
        assert pois[poi_id]["category"] == "public_space"


def test_seich_sou_has_required_safety_classification() -> None:
    forest = by_id()["seich_sou_forest"]
    assert forest["category"] == "trail_forest"
    assert forest["exposure"] == "outdoor"
    assert forest["heat_exposure"] == "high"
    assert forest["safety_tier"] == "critical"
    assert all(term in forest["notes"] for term in ("storm", "night", "fire-risk"))


def test_holidays_are_explicit_and_unverified() -> None:
    holidays = load_yaml("holidays.yaml")["holidays"]
    assert all(item["needs_verification"] is True for item in holidays)
    assert {item["date"] for item in holidays if item.get("easter_dependent")} == {
        date(2026, 2, 23),
        date(2026, 4, 10),
        date(2026, 4, 12),
        date(2026, 4, 13),
        date(2026, 5, 31),
        date(2026, 6, 1),
    }
    assert any(
        item["date"] == date(2026, 10, 26) and item["scope"] == "thessaloniki_local"
        for item in holidays
    )
