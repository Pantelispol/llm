from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.domain.catalog import PoiCatalog
from app.domain.models import Intent, TripState, TurnAnalysis
from app.domain.ports import LLMProvider, LLMUsage
from app.llm.openai_provider import LLMProviderError, OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget, ReplayFixtureError

PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "understand.v1.md"
PROMPT_VERSION = "understand.v1"
FALLBACK_QUESTION = (
    "Could you clarify whether you want destination information or a change to your plan?"
)

logger = logging.getLogger(__name__)


class UnderstandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: TurnAnalysis
    used_fallback: bool = False
    failure_category: str | None = None
    usage: LLMUsage | None = None


def canonical_dynamic_input(user_turn: str, trip_state: TripState) -> str:
    payload = {
        "trip_state": trip_state.model_dump(mode="json"),
        "user_turn": user_turn,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[TURN_INPUT]\n{canonical}\n[/TURN_INPUT]"


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[\w]+", without_marks, flags=re.UNICODE))


def _contains_term(text: str, terms: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {_normalized(term)} " in padded for term in terms)


def _alias_index(catalog: PoiCatalog) -> dict[str, str]:
    index: dict[str, str] = {}
    for poi in catalog.pois:
        for name in (poi.id, poi.names.en, poi.names.el, *poi.aliases):
            normalized = _normalized(name)
            existing = index.get(normalized)
            if existing is not None and existing != poi.id:
                raise ValueError(f"catalog alias is ambiguous after normalization: {name}")
            index[normalized] = poi.id
    return index


def _literal_unknown_place(user_turn: str) -> str | None:
    patterns = (
        r"^\s*(?:tell me about|what is|what's)\s+(.+?)[?.!]*\s*$",
        r"^\s*(?:πες μου για|τι ειναι)\s+(.+?)[?.!·]*\s*$",
    )
    normalized_turn = unicodedata.normalize("NFKD", user_turn)
    normalized_turn = "".join(
        char for char in normalized_turn if not unicodedata.combining(char)
    )
    for pattern in patterns:
        match = re.match(pattern, normalized_turn, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


class Understander:
    def __init__(
        self,
        provider: LLMProvider,
        catalog: PoiCatalog,
        *,
        system_prompt: str | None = None,
    ) -> None:
        self.provider = provider
        self.catalog = catalog
        self.system_prompt = system_prompt or PROMPT_PATH.read_text(encoding="utf-8")
        self._known_ids = {poi.id for poi in catalog.pois}
        self._aliases = _alias_index(catalog)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        catalog: PoiCatalog,
        *,
        call_name: str = "understand",
        fixture_store: FixtureStore | None = None,
        client: Any | None = None,
        live_call_budget: LiveCallBudget | None = None,
    ) -> Understander:
        provider = OpenAIProvider.from_settings(
            settings,
            profile="understand",
            call_name=call_name,
            prompt_version=PROMPT_VERSION,
            catalog=catalog,
            fixture_store=fixture_store,
            client=client,
            live_call_budget=live_call_budget,
        )
        return cls(provider, catalog)

    async def analyze(self, user_turn: str, trip_state: TripState) -> UnderstandResult:
        dynamic_input = canonical_dynamic_input(user_turn, trip_state)
        try:
            result = await self.provider.structured(
                system_prompt=self.system_prompt,
                user_prompt=dynamic_input,
                output_type=TurnAnalysis,
            )
            analysis = self._resolve_and_filter_catalog_ids(result.output)
            return UnderstandResult(analysis=analysis, usage=result.usage)
        except (LLMProviderError, ReplayFixtureError, TimeoutError) as error:
            category = type(error).__name__
            logger.warning("understand provider failure category=%s", category)
            return UnderstandResult(
                analysis=self._fallback(user_turn, trip_state),
                used_fallback=True,
                failure_category=category,
            )

    def _resolve_and_filter_catalog_ids(self, analysis: TurnAnalysis) -> TurnAnalysis:
        payload = analysis.model_dump(mode="json")
        entities = payload["entities"]
        mentioned = [
            poi_id for poi_id in entities["mentioned_poi_ids"] if poi_id in self._known_ids
        ]
        unresolved = []
        for place_name in entities["unresolved_place_names"]:
            resolved = self._aliases.get(_normalized(place_name))
            if resolved is None:
                unresolved.append(place_name)
            else:
                mentioned.append(resolved)
        entities["mentioned_poi_ids"] = list(dict.fromkeys(mentioned))
        entities["unresolved_place_names"] = list(dict.fromkeys(unresolved))

        updates = payload["constraint_updates"]
        for field in ("add_exclude_poi_ids", "remove_exclude_poi_ids", "add_visited"):
            updates[field] = [poi_id for poi_id in updates[field] if poi_id in self._known_ids]

        safe_edits: list[dict[str, Any]] = []
        for edit in payload["plan_edits"]:
            preferred = edit.get("preferred_poi_id")
            target = edit.get("poi_id")
            if preferred is not None and preferred not in self._known_ids:
                edit["preferred_poi_id"] = None
            if target is not None and target not in self._known_ids:
                continue
            if (
                edit["op"] == "add_activity"
                and edit.get("preferred_poi_id") is None
                and not edit.get("required_tags")
                and edit.get("required_exposure") is None
            ):
                continue
            safe_edits.append(edit)
        payload["plan_edits"] = safe_edits
        return TurnAnalysis.model_validate(payload)

    def _fallback(self, user_turn: str, trip_state: TripState) -> TurnAnalysis:
        text = _normalized(user_turn)
        known_mentions = []
        for alias, poi_id in sorted(self._aliases.items(), key=lambda item: -len(item[0])):
            if f" {alias} " in f" {text} ":
                known_mentions.append(poi_id)

        intents: list[Intent]
        if _contains_term(
            text,
            (
                "opening hours",
                "open",
                "opens",
                "close",
                "closes",
                "ωραριο",
                "ανοιχτα",
                "ανοιγει",
                "κλεινει",
            ),
        ):
            intents = [Intent.OPENING_HOURS_QUESTION]
        elif _contains_term(
            text,
            (
                "admission",
                "ticket",
                "tickets",
                "cost",
                "price",
                "τιμη",
                "κοστος",
                "κοστιζει",
                "εισιτηριο",
            ),
        ):
            intents = [Intent.PRICE_QUESTION]
        elif _contains_term(text, ("weather", "forecast", "καιρο", "καιρος", "καιρου")):
            intents = [Intent.WEATHER_QUESTION]
        elif _contains_term(text, ("feasible", "feasibility", "fit", "possible", "προλαβαινω")):
            intents = [Intent.FEASIBILITY_CHECK]
        elif _contains_term(
            text,
            ("change", "replace", "remove", "add", "edit", "instead", "αλλαξε", "βγαλε"),
        ) or (
            trip_state.itinerary is not None
            and _contains_term(text, ("no museum", "not museum"))
        ):
            intents = [Intent.EDIT_PLAN]
        elif _contains_term(
            text,
            (
                "create plan",
                "create a plan",
                "plan a",
                "make a plan",
                "itinerary",
                "schedule",
                "προγραμμα",
            ),
        ):
            intents = [Intent.CREATE_PLAN]
        else:
            unknown = _literal_unknown_place(user_turn)
            return TurnAnalysis(
                intents=[Intent.OUT_OF_SCOPE],
                entities={
                    "mentioned_poi_ids": list(dict.fromkeys(known_mentions)),
                    "unresolved_place_names": [unknown] if unknown and not known_mentions else [],
                },
                needs_clarification=True,
                clarifying_question=FALLBACK_QUESTION,
            )

        return TurnAnalysis(
            intents=intents,
            entities={"mentioned_poi_ids": list(dict.fromkeys(known_mentions))},
        )
