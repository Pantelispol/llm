from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Intent, TripState, TurnAnalysis
from app.llm.openai_provider import LLMTransportError
from app.main import app, get_now, get_pipeline
from tests.conftest import RaisingProvider
from tests.test_pipeline import ASSIGNMENT_TURNS, assignment_analyses

ATHENS = ZoneInfo("Europe/Athens")
NOW = datetime(2026, 9, 21, 12, tzinfo=ATHENS)


@pytest.fixture
def client(stub_pipeline):
    """Every port is replaced through FastAPI's dependency overrides."""

    def make(**kwargs):
        pipeline = stub_pipeline(**kwargs)
        app.dependency_overrides[get_pipeline] = lambda: pipeline
        app.dependency_overrides[get_now] = lambda: NOW
        return TestClient(app), pipeline

    yield make
    app.dependency_overrides.clear()


def test_health_endpoint_still_responds(client):
    http, _ = client(analyses=[TurnAnalysis(intents=[Intent.SMALL_TALK])])
    response = http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_endpoint_round_trips_trip_state(client):
    http, _ = client(analyses=assignment_analyses())

    state = TripState().model_dump(mode="json")
    versions = []
    for turn in ASSIGNMENT_TURNS:
        response = http.post("/chat", json={"message": turn, "trip_state": state})
        assert response.status_code == 200, response.text
        payload = response.json()
        state = payload["trip_state"]
        versions.append(payload["itinerary_version"])
        assert payload["answer"].strip()
        assert TripState.model_validate(state) is not None

    assert versions == [1, 2, 3]
    final = TripState.model_validate(state)
    assert final.exclude_categories == ["museum"]
    assert final.party.children_ages == [10]
    # There is no transcript field: the client carries state, not history.
    assert "history" not in payload
    assert "transcript" not in payload


def test_chat_endpoint_omitting_trip_state_starts_a_new_conversation(client):
    http, _ = client(analyses=assignment_analyses()[:1])
    response = http.post("/chat", json={"message": ASSIGNMENT_TURNS[0]})
    assert response.status_code == 200
    assert response.json()["itinerary_version"] == 1


def test_chat_endpoint_rejects_invalid_trip_state_with_422(client):
    http, _ = client(analyses=assignment_analyses()[:1])

    malformed = http.post(
        "/chat",
        json={"message": "five hours tomorrow", "trip_state": {"pace": "supersonic"}},
    )
    assert malformed.status_code == 422

    inconsistent = http.post(
        "/chat",
        json={"message": "five hours tomorrow", "trip_state": {"itinerary_version": 4}},
    )
    assert inconsistent.status_code == 422

    assert http.post("/chat", json={"message": ""}).status_code == 422


def test_provider_failure_uses_fallback_instead_of_http_500(client):
    http, _ = client(
        analyses=assignment_analyses()[:1],
        narrate_provider=RaisingProvider(LLMTransportError("synthetic failure")),
    )
    response = http.post("/chat", json={"message": ASSIGNMENT_TURNS[0]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["used_fallback"] is True
    assert payload["failure_category"] == "LLMTransportError"
    assert payload["answer"].strip()
    assert payload["itinerary_version"] == 1


def test_chat_response_carries_usage_and_tool_trace(client):
    http, _ = client(analyses=assignment_analyses()[:1])
    payload = http.post("/chat", json={"message": ASSIGNMENT_TURNS[0]}).json()

    assert payload["tools_called"] == [
        "catalog",
        "weather",
        "hours",
        "travel",
        "planner",
        "validator",
    ]
    assert len(payload["usages"]) == 2
    assert {usage["reasoning_effort"] for usage in payload["usages"]} == {"none", "low"}
