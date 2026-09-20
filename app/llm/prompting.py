from __future__ import annotations

import json
from typing import Any

from app.domain.catalog import PoiCatalog


def canonical_catalog_summary(catalog: PoiCatalog) -> str:
    """Return a stable, compact catalog representation for the cached prompt prefix."""
    pois: list[dict[str, Any]] = []
    for poi in sorted(catalog.pois, key=lambda item: item.id):
        pois.append(
            {
                "aliases": sorted(poi.aliases),
                "category": poi.category,
                "child_friendly": poi.child_friendly.value,
                "exposure": poi.exposure.value,
                "id": poi.id,
                "names": {"el": poi.names.el, "en": poi.names.en},
                "safety_tier": poi.safety_tier.value,
                "step_free": poi.step_free,
                "tags": sorted(poi.tags),
                "visit_minutes": poi.visit_minutes.model_dump(mode="json"),
            }
        )
    payload = {
        "city": catalog.city,
        "pois": pois,
        "schema_version": catalog.schema_version,
        "timezone": catalog.timezone,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_stable_prefix(system_prompt: str, catalog: PoiCatalog) -> str:
    instructions = system_prompt.strip()
    summary = canonical_catalog_summary(catalog)
    return (
        f"{instructions}\n\n"
        "[CATALOG_SUMMARY]\n"
        f"{summary}\n"
        "[/CATALOG_SUMMARY]"
    )
