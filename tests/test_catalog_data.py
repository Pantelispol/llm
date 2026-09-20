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
    "last_entry_before_close_min",
    "child_friendly",
    "step_free",
    "heat_exposure",
    "price_eur",
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


def test_archaeological_museum_uses_corrected_official_operational_data() -> None:
    museum = by_id()["archaeological_museum"]
    assert museum["opening_hours"] == [
        {
            "valid_from": "04-01",
            "valid_to": "10-31",
            "expression": "Mo-Su 09:00-17:00",
        },
        {
            "valid_from": "11-01",
            "valid_to": "03-31",
            "expression": "Mo,We-Su 09:00-17:00; Tu off",
        },
    ]
    assert museum["last_entry_before_close_min"] == 20
    assert museum["price_eur"] == {"standard": 10, "reduced": 5}
    assert museum["needs_verification"]["opening_hours"] is False
    assert museum["source"] == {
        "url": "https://www.amth.gr/en/visit/hours-and-tickets",
        "kind": "official",
        "verified_at": date(2026, 9, 20),
    }
    assert len(museum["conflicts"]) == 2
    assert {item["source"]["kind"] for item in museum["conflicts"]} == {"third_party"}


def test_white_tower_and_byzantine_museum_corrections_are_explicit() -> None:
    pois = by_id()
    tower = pois["white_tower"]
    assert tower["temporary_closures"] == [
        {
            "from": date(2026, 10, 12),
            "to": date(2026, 10, 15),
            "reason": "construction works",
        }
    ]
    assert tower["needs_verification"]["opening_hours"] is False

    museum = pois["museum_of_byzantine_culture"]
    assert museum["opening_hours"][0]["valid_from"] == "05-08"
    assert museum["needs_verification"]["opening_hours"] is True
    assert museum["last_entry_before_close_min"] == 20
    assert museum["needs_verification"]["last_entry_before_close_min"] is True


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


def test_manually_verified_hours_include_provenance() -> None:
    verified = {
        "rotunda",
        "roman_forum",
        "hagios_demetrios",
        "acheiropoietos",
        "hagia_sophia",
        "heptapyrgio",
        "jewish_museum",
        "modiano_market",
        "kapani_market",
    }
    pois = by_id()
    for poi_id in verified:
        poi = pois[poi_id]
        assert poi["opening_hours"]
        assert poi["needs_verification"]["opening_hours"] is False
        assert poi["source"]["verified_by"] == "Tony (manual check)"
        assert poi["source"]["verified_at"] == date(2026, 9, 20)
        assert poi["source"]["confidence"] in {"low", "medium"}


def test_active_churches_have_visitor_note() -> None:
    expected = "active church: modest dress, quiet during services"
    for poi_id in ("rotunda", "hagios_demetrios", "acheiropoietos", "hagia_sophia"):
        assert by_id()[poi_id]["visitor_note"] == expected


def test_public_space_classification_audit_and_white_tower_exposure() -> None:
    pois = by_id()
    for poi_id in ("ano_poli_walls", "arch_of_galerius"):
        assert pois[poi_id]["category"] == "public_space"
        assert pois[poi_id]["opening_hours"] == [
            {
                "valid_from": "01-01",
                "valid_to": "12-31",
                "expression": "24/7",
            }
        ]
        assert pois[poi_id]["needs_verification"]["opening_hours"] is False
    assert pois["white_tower"]["exposure"] == "indoor"


def test_holidays_are_explicit_and_unverified() -> None:
    holidays = load_yaml("holidays.yaml")["holidays"]
    assert all(item["needs_verification"] is True for item in holidays)
    assert len({item["id"] for item in holidays}) == len(holidays)
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
