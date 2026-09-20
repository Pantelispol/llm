"""The conversation pipeline.

One turn is `understand → route → gather → plan → validate → narrate →
post-check`, sequenced here explicitly rather than inside a framework callback
or a model tool loop. `TripState` is the only thing carried between turns: no
transcript, no assistant prose, no growing message list, so prompt size is
bounded by the state rather than by how long the conversation has run.

Plan-touching turns are transactional. Constraint updates are applied to a
working copy, planning and validation run against that copy, and the itinerary
and its version are committed exactly once — and only after the independent
validator passes. A failed turn returns typed violations and the state the
traveler already had.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings
from app.domain.models import (
    AnswerLanguage,
    EvidenceItem,
    Intent,
    Itinerary,
    NarrationBundle,
    NarrationDisclosure,
    TripState,
    TurnAnalysis,
    ValidationResult,
    Violation,
    ViolationSeverity,
)
from app.domain.ports import (
    LLMUsage,
    OpeningHoursChecker,
    PlanningContext,
    Retriever,
    TravelMatrixRequest,
    TravelMatrixResult,
    TravelTimeProvider,
    WeatherProvider,
    WeatherRequest,
)
from app.llm.narrate import NarrationResult, Narrator
from app.llm.understand import Understander
from app.orchestrator.evidence import (
    admission_evidence,
    approximate_travel_evidence,
    hours_evidence,
    retrieval_evidence,
    weather_summary_evidence,
    weather_unavailable_evidence,
)
from app.orchestrator.feasibility import (
    NEEDS_INPUT_ANSWER,
    feasibility_violations,
    hypothesis_poi_ids,
    is_feasibility_only,
    render_feasibility_answer,
)
from app.orchestrator.language import resolve_turn_language
from app.orchestrator.models import (
    GatheredContext,
    HoursFact,
    PipelineResult,
    PipelineStage,
    RouteDecision,
    ToolName,
    TravelProvenance,
    WeatherProvenance,
)
from app.orchestrator.router import route_turn
from app.orchestrator.temporal import (
    resolve_feasibility_window,
    resolve_time_window,
    to_athens,
)
from app.planning.feasibility import FeasibilityChecker
from app.planning.planner import BeamSearchPlanner, PlanResult
from app.planning.repair import PlanRepairer, apply_constraint_updates
from app.planning.rules import pace_factors_for
from app.planning.validator import DeterministicItineraryValidator
from app.safety.answer_postcheck import PostCheckReport, check_answer
from app.tools.catalog import CatalogRepository
from app.tools.weather_flags import derive_weather_flags, thresholds_from_settings

ATHENS = ZoneInfo("Europe/Athens")
CITY_REFERENCE_POI = "aristotelous_square"
DEFAULT_CONVERSATION_FIXTURE_ROOT = (
    Path(__file__).parents[2] / "evals" / "fixtures" / "llm" / "conversation"
)
DEFAULT_WEATHER_START_HOUR = 6
DEFAULT_WEATHER_END_HOUR = 23

logger = logging.getLogger(__name__)

VALIDATION_FAILURE_ANSWER = {
    AnswerLanguage.EN: (
        "I could not build a schedule that satisfies every constraint, so nothing was "
        "changed. The blocking checks were: {codes}."
    ),
    AnswerLanguage.EL: (
        "Δεν μπόρεσα να φτιάξω πρόγραμμα που να ικανοποιεί όλους τους περιορισμούς, "
        "οπότε δεν άλλαξε τίποτα. Οι έλεγχοι που απέτυχαν ήταν: {codes}."
    ),
}


class ConversationPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: CatalogRepository,
        hours: OpeningHoursChecker,
        travel: TravelTimeProvider,
        weather: WeatherProvider,
        understander: Understander,
        narrator: Narrator,
        retriever: Retriever | None = None,
        planner: BeamSearchPlanner | None = None,
        repairer: PlanRepairer | None = None,
        validator: DeterministicItineraryValidator | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.catalog = repository.catalog
        self.hours = hours
        self.travel = travel
        self.weather = weather
        self.understander = understander
        self.narrator = narrator
        self.retriever = retriever
        self.planner = planner or BeamSearchPlanner()
        self.repairer = repairer or PlanRepairer(self.planner)
        self.validator = validator or DeterministicItineraryValidator()
        self.feasibility = FeasibilityChecker()
        self._known_poi_ids = {poi.id for poi in self.catalog.pois}

    async def run_turn(
        self,
        user_turn: str,
        trip_state: TripState,
        *,
        now: datetime,
    ) -> PipelineResult:
        stages: list[PipelineStage] = []
        language = resolve_turn_language(user_turn)
        athens_now = to_athens(now)

        # --- understand -------------------------------------------------
        stages.append(PipelineStage.UNDERSTAND)
        understood = await self.understander.analyze(user_turn, trip_state)
        analysis = understood.analysis
        usages: list[LLMUsage] = [understood.usage] if understood.usage else []

        window, assumptions = resolve_time_window(
            user_turn,
            analysis.entities.requested_date_text,
            athens_now,
            day_start_hour=self.settings.default_day_start_hour,
        )

        # --- route ------------------------------------------------------
        # Route on the model's own analysis. The deterministically resolved
        # window is folded in afterwards: a date word ("tomorrow") in a question
        # must not read as a plan edit.
        stages.append(PipelineStage.ROUTE)
        route = route_turn(user_turn, analysis, trip_state)
        if is_feasibility_only(analysis):
            return await self._feasibility_turn(
                user_turn=user_turn,
                analysis=analysis,
                trip_state=trip_state,
                route=route,
                language=language,
                now=athens_now,
                stages=stages,
                usages=usages,
                understand_fallback=understood.used_fallback,
                understand_failure_category=understood.failure_category,
            )
        analysis = self._with_resolved_window(analysis, window, trip_state)

        projected = apply_constraint_updates(trip_state, analysis.constraint_updates)
        plan_date = (
            projected.time_window.start.astimezone(ATHENS).date()
            if projected.time_window
            else athens_now.date()
        )

        # --- gather -----------------------------------------------------
        stages.append(PipelineStage.GATHER)
        gathered = await self._gather(
            user_turn=user_turn,
            analysis=analysis,
            state=projected,
            route=route,
            now=athens_now,
            plan_date=plan_date,
        )

        # --- plan -------------------------------------------------------
        stages.append(PipelineStage.PLAN)
        proposal: PlanResult | None = None
        candidate_state = projected
        can_plan = projected.time_window is not None or trip_state.itinerary is not None
        if route.touches_plan and gathered.planning_context is not None and can_plan:
            proposal, candidate_state = self._propose_plan(
                trip_state, analysis, gathered.planning_context
            )
            gathered.tools_called.append(ToolName.PLANNER)
        elif route.touches_plan:
            # The planner requires a window. Asking for one beats raising: a turn
            # routed to planning without a time the traveler has stated is a
            # question the assistant should answer in prose, not a 500.
            logger.info("plan stage skipped: no time window and no existing itinerary")

        # --- validate ---------------------------------------------------
        stages.append(PipelineStage.VALIDATE)
        validation: ValidationResult | None = None
        if proposal is not None and gathered.planning_context is not None:
            gathered.tools_called.append(ToolName.VALIDATOR)
            validation = self.validator.validate(
                proposal.itinerary, candidate_state, gathered.planning_context
            )
            if not validation.is_valid:
                return self._validation_failure(
                    stages=stages,
                    language=language,
                    trip_state=trip_state,
                    route=route,
                    gathered=gathered,
                    validation=validation,
                    usages=usages,
                    understand_fallback=understood.used_fallback,
            understand_failure_category=understood.failure_category,
                )

        committed_state, committed_plan = self._commit(
            trip_state=trip_state,
            candidate_state=candidate_state,
            proposal=proposal,
            route=route,
            assumptions=assumptions,
        )
        if committed_plan is not None:
            self._extend_hours_evidence(gathered, committed_plan, plan_date)

        # --- narrate + post-check ---------------------------------------
        stages.append(PipelineStage.NARRATE)
        bundle = NarrationBundle(
            request_id=f"turn-{committed_state.itinerary_version}-{plan_date.isoformat()}",
            language=language,
            user_question=user_turn,
            plan=committed_plan,
            validation=validation if committed_plan is not None else None,
            catalog_poi_ids=gathered.catalog_poi_ids,
            evidence=gathered.evidence,
            disclosures=gathered.disclosures,
        )
        narrated = await self._narrate(bundle, stages)
        usages.extend(narrated.usages)

        return PipelineResult(
            answer=narrated.answer,
            trip_state=committed_state,
            language=language,
            stages=stages,
            route=route,
            tools_called=gathered.tools_called,
            itinerary_version=committed_state.itinerary_version,
            plan_committed=committed_plan is not None,
            violations=list(validation.violations) if validation else [],
            disclosures=gathered.disclosures,
            citations=list(narrated.citations),
            used_fallback=narrated.used_fallback,
            understand_fallback=understood.used_fallback,
            failure_category=narrated.failure_category,
            postcheck_codes=narrated.violation_codes,
            narration_invoked=True,
            allowed_poi_ids=sorted(bundle.allowed_poi_ids),
            evidence_ids=[item.evidence_id for item in bundle.evidence],
            narration_first_draft_passed=narrated.first_draft_passed,
            narration_retried=narrated.retried,
            first_draft_codes=[
                item.code.value for item in narrated.first_draft_violations
            ],
            needs_clarification=analysis.needs_clarification,
            usages=usages,
            assumptions=list(committed_state.assumptions),
        )

    async def _feasibility_turn(
        self,
        *,
        user_turn: str,
        analysis: TurnAnalysis,
        trip_state: TripState,
        route: RouteDecision,
        language: AnswerLanguage,
        now: datetime,
        stages: list[PipelineStage],
        usages: list[LLMUsage],
        understand_fallback: bool,
        understand_failure_category: str | None,
    ) -> PipelineResult:
        """Answer "does this fit?" for the traveler's own inputs; commit nothing.

        The window is the traveler's stated arrival or range, falling back to the
        stored window. The planner and validator are not called: the checker is
        the authority on this question and there is no plan to validate.
        """
        requested = analysis.entities.requested_date_text
        window, assumptions = resolve_feasibility_window(user_turn, requested, now)
        if window is None:
            window, assumptions = resolve_time_window(
                user_turn, requested, now, day_start_hour=self.settings.default_day_start_hour
            )
        if window is None:
            window = trip_state.time_window
        poi_ids = hypothesis_poi_ids(analysis, trip_state, self._known_poi_ids)

        route = route.model_copy(
            update={
                "tools": [
                    tool
                    for tool in route.tools
                    if tool not in {ToolName.PLANNER, ToolName.VALIDATOR}
                ]
            }
        )
        stages.append(PipelineStage.GATHER)
        if window is None or not poi_ids:
            return PipelineResult(
                answer=NEEDS_INPUT_ANSWER[language],
                trip_state=trip_state,
                language=language,
                stages=stages,
                route=route,
                itinerary_version=trip_state.itinerary_version,
                used_fallback=True,
                understand_fallback=understand_fallback,
                understand_failure_category=understand_failure_category,
                needs_clarification=True,
                usages=usages,
            )

        working = apply_constraint_updates(trip_state, analysis.constraint_updates).model_copy(
            update={"time_window": window}
        )
        plan_date = window.start.astimezone(ATHENS).date()
        focused = analysis.model_copy(
            update={
                "entities": analysis.entities.model_copy(update={"mentioned_poi_ids": poi_ids})
            }
        )
        gathered = await self._gather(
            user_turn=user_turn,
            analysis=focused,
            state=working,
            route=route,
            now=now,
            plan_date=plan_date,
        )
        context = gathered.planning_context
        assert context is not None  # feasibility turns are routed through the plan tools

        stages.append(PipelineStage.FEASIBILITY)
        result = self.feasibility.check(poi_ids, working, context)
        gathered.tools_called.append(ToolName.FEASIBILITY)

        answer = render_feasibility_answer(
            result,
            catalog=self.catalog,
            hours=self.hours,
            language=language,
            window_end=window.end,
        )
        return PipelineResult(
            answer=answer,
            trip_state=trip_state,
            language=language,
            stages=stages,
            route=route,
            tools_called=gathered.tools_called,
            itinerary_version=trip_state.itinerary_version,
            plan_committed=False,
            violations=feasibility_violations(result, self.catalog),
            disclosures=gathered.disclosures,
            used_fallback=True,
            understand_fallback=understand_fallback,
            understand_failure_category=understand_failure_category,
            narration_invoked=False,
            allowed_poi_ids=sorted(poi_ids),
            evidence_ids=[item.evidence_id for item in gathered.evidence],
            usages=usages,
            assumptions=assumptions,
            feasibility=result,
        )

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------

    def _with_resolved_window(
        self,
        analysis: TurnAnalysis,
        window: object,
        trip_state: TripState,
    ) -> TurnAnalysis:
        """Fold the deterministically resolved window into the routed deltas."""
        updates = analysis.constraint_updates
        if window is None or updates.clear_time_window or window == trip_state.time_window:
            return analysis
        return analysis.model_copy(
            update={"constraint_updates": updates.model_copy(update={"time_window": window})}
        )

    async def _gather(
        self,
        *,
        user_turn: str,
        analysis: TurnAnalysis,
        state: TripState,
        route: RouteDecision,
        now: datetime,
        plan_date: date,
    ) -> GatheredContext:
        gathered = GatheredContext(now=now, plan_date=plan_date)
        focus = self._focus_poi_ids(analysis, state)
        weather_flags = []
        weather_unavailable: str | None = None
        travel_matrix: TravelMatrixResult | None = None

        for tool in route.tools:
            if tool == ToolName.CATALOG:
                gathered.tools_called.append(tool)
                gathered.catalog_poi_ids = focus
                for poi_id in focus:
                    admission = admission_evidence(self.repository, poi_id)
                    if admission is not None and Intent.PRICE_QUESTION in route.effective_intents:
                        gathered.evidence.append(admission)
            elif tool == ToolName.RETRIEVER and self.retriever is not None:
                gathered.tools_called.append(tool)
                hits = await self.retriever.search(
                    user_turn, limit=self.settings.retrieval_limit
                )
                gathered.retrieval = list(hits)
                gathered.evidence.extend(retrieval_evidence(hit) for hit in hits)
                if hits:
                    gathered.disclosures.append(NarrationDisclosure.DRAFT_CONTENT)
            elif tool == ToolName.WEATHER:
                gathered.tools_called.append(tool)
                weather_flags, weather_unavailable = await self._weather(
                    gathered, state, plan_date
                )
            elif tool == ToolName.HOURS:
                gathered.tools_called.append(tool)
                self._hours(gathered, focus, plan_date)
            elif tool == ToolName.TRAVEL:
                gathered.tools_called.append(tool)
                travel_matrix = self._travel(gathered)

        if route.touches_plan:
            if travel_matrix is None:
                travel_matrix = self._travel(gathered)
            gathered.planning_context = PlanningContext(
                now=now,
                catalog=self.catalog,
                opening_hours=self.hours,
                candidates=[],
                hourly_weather_flags=weather_flags,
                weather_unavailable_reason=weather_unavailable,
                travel_matrix=travel_matrix,
                pace_factors=pace_factors_for(state),
            )
        return gathered

    def _focus_poi_ids(self, analysis: TurnAnalysis, state: TripState) -> list[str]:
        ids = [
            poi_id
            for poi_id in analysis.entities.mentioned_poi_ids
            if poi_id in self._known_poi_ids
        ]
        if state.itinerary is not None:
            ids.extend(
                activity.poi_id
                for activity in state.itinerary.activities
                if activity.poi_id in self._known_poi_ids
            )
        return list(dict.fromkeys(ids))

    async def _weather(
        self,
        gathered: GatheredContext,
        state: TripState,
        plan_date: date,
    ) -> tuple[list, str | None]:
        reference = self.repository.get(CITY_REFERENCE_POI)
        if state.time_window is not None:
            start, end = state.time_window.start, state.time_window.end
        else:
            start = datetime.combine(
                plan_date, time(DEFAULT_WEATHER_START_HOUR), tzinfo=ATHENS
            )
            end = datetime.combine(plan_date, time(DEFAULT_WEATHER_END_HOUR), tzinfo=ATHENS)
        result = await self.weather.forecast(
            WeatherRequest(location=reference.coordinates, start=start, end=end)
        )
        report = derive_weather_flags(
            result, state.party, thresholds_from_settings(self.settings)
        )
        gathered.weather = WeatherProvenance(
            source=result.source,
            is_fixture=result.is_fixture,
            unavailable_reason=result.unavailable_reason,
            summary=report.summary,
        )
        if result.unavailable_reason:
            # Missing weather is never treated as safe weather.
            gathered.evidence.append(
                weather_unavailable_evidence(plan_date, result.unavailable_reason)
            )
            gathered.disclosures.append(NarrationDisclosure.WEATHER_UNAVAILABLE)
        elif report.summary:
            gathered.evidence.append(
                weather_summary_evidence(plan_date, result.source, report.summary)
            )
        return report.hours, result.unavailable_reason

    def _hours(
        self, gathered: GatheredContext, poi_ids: list[str], plan_date: date
    ) -> None:
        for poi_id in poi_ids:
            interval = self.hours.next_open_interval(
                poi_id, datetime.combine(plan_date, time(0, 1), tzinfo=ATHENS)
            )
            if interval is None:
                continue
            open_today = interval.opens_at.astimezone(ATHENS).date() == plan_date
            gathered.hours.append(
                HoursFact(
                    poi_id=poi_id,
                    opens_at=interval.opens_at,
                    closes_at=interval.closes_at,
                    last_entry_at=interval.last_entry_at,
                    source_rule=interval.source_rule,
                    needs_verification=interval.needs_verification,
                    open_on_requested_day=open_today,
                )
            )
            if interval.needs_verification:
                gathered.disclosures.append(NarrationDisclosure.UNVERIFIED_HOURS)
            self._append_evidence(gathered, hours_evidence(self.hours, poi_id, plan_date))

    def _travel(self, gathered: GatheredContext) -> TravelMatrixResult:
        locations = {poi.id: poi.coordinates for poi in self.catalog.pois}
        matrix = self.travel.matrix(TravelMatrixRequest(locations=locations))
        approximate = any(leg.approximate for leg in matrix.legs)
        gathered.travel = TravelProvenance(source=matrix.source, approximate=approximate)
        if approximate:
            gathered.evidence.append(approximate_travel_evidence(matrix.source))
            gathered.disclosures.append(NarrationDisclosure.APPROXIMATE_TRAVEL)
        return matrix

    @staticmethod
    def _append_evidence(gathered: GatheredContext, item: EvidenceItem | None) -> None:
        if item is None:
            return
        if any(existing.evidence_id == item.evidence_id for existing in gathered.evidence):
            return
        gathered.evidence.append(item)

    def _extend_hours_evidence(
        self,
        gathered: GatheredContext,
        plan: Itinerary,
        plan_date: date,
    ) -> None:
        """The hours engine also answers for the POIs planning actually chose."""
        for activity in plan.activities:
            if activity.poi_id is None:
                continue
            self._append_evidence(
                gathered, hours_evidence(self.hours, activity.poi_id, plan_date)
            )
            if activity.poi_id not in gathered.catalog_poi_ids:
                gathered.catalog_poi_ids.append(activity.poi_id)

    def _propose_plan(
        self,
        trip_state: TripState,
        analysis: TurnAnalysis,
        context: PlanningContext,
    ) -> tuple[PlanResult, TripState]:
        updates = analysis.constraint_updates
        if trip_state.itinerary is None:
            working = apply_constraint_updates(trip_state, updates)
            return self.planner.plan(working, context), working
        previous = self._rehydrate(trip_state, context)
        repaired = self.repairer.repair(
            previous,
            trip_state,
            context,
            operations=list(analysis.plan_edits),
            constraint_updates=updates,
        )
        return repaired.plan, repaired.state

    def _rehydrate(self, trip_state: TripState, context: PlanningContext) -> PlanResult:
        """Rebuild a plan result from stored state; state only ever holds valid plans."""
        itinerary = trip_state.itinerary
        assert itinerary is not None
        return PlanResult(
            itinerary=itinerary,
            score_breakdown=[],
            dropped_candidates=[],
            assumptions=list(trip_state.assumptions),
            validation=self.validator.validate(itinerary, trip_state, context),
            total_score=0.0,
            window_utilization=_window_utilization(itinerary),
            utilization_score=0.0,
        )

    def _commit(
        self,
        *,
        trip_state: TripState,
        candidate_state: TripState,
        proposal: PlanResult | None,
        route: RouteDecision,
        assumptions: list[str],
    ) -> tuple[TripState, Itinerary | None]:
        if proposal is None:
            # No proposal means no itinerary change and no version increment.
            return trip_state, trip_state.itinerary
        merged = list(
            dict.fromkeys([*candidate_state.assumptions, *assumptions, *proposal.assumptions])
        )
        committed = candidate_state.model_copy(
            update={
                "itinerary": proposal.itinerary,
                "itinerary_version": trip_state.itinerary_version + 1,
                "assumptions": merged,
            }
        )
        return committed, proposal.itinerary

    async def _narrate(
        self, bundle: NarrationBundle, stages: list[PipelineStage]
    ) -> NarrationResult:
        """Run narration with a per-call checker so the post-check is an observed stage."""

        def checker(draft: str, narration_bundle: NarrationBundle, catalog) -> PostCheckReport:
            if PipelineStage.POST_CHECK not in stages:
                stages.append(PipelineStage.POST_CHECK)
            return check_answer(draft, narration_bundle, catalog)

        return await self.narrator.narrate(bundle, checker=checker)

    def _validation_failure(
        self,
        *,
        stages: list[PipelineStage],
        language: AnswerLanguage,
        trip_state: TripState,
        route: RouteDecision,
        gathered: GatheredContext,
        validation: ValidationResult,
        usages: list[LLMUsage],
        understand_fallback: bool,
        understand_failure_category: str | None,
    ) -> PipelineResult:
        """Stop before narration and hand back typed violations with unchanged state."""
        blocking: list[Violation] = [
            item
            for item in validation.violations
            if item.severity == ViolationSeverity.ERROR
        ] or list(validation.violations)
        codes = ", ".join(dict.fromkeys(item.code.value for item in blocking))
        logger.info("pipeline stopped before narration: %s", codes)
        return PipelineResult(
            answer=VALIDATION_FAILURE_ANSWER[language].format(codes=codes),
            trip_state=trip_state,
            language=language,
            stages=stages,
            route=route,
            tools_called=gathered.tools_called,
            itinerary_version=trip_state.itinerary_version,
            plan_committed=False,
            violations=list(validation.violations),
            disclosures=gathered.disclosures,
            used_fallback=True,
            understand_fallback=understand_fallback,
            understand_failure_category=understand_failure_category,
            failure_category="validation_failed",
            usages=usages,
        )


def _window_utilization(itinerary: Itinerary) -> float:
    span = (itinerary.window.end - itinerary.window.start).total_seconds()
    if span <= 0:
        return 0.0
    used = sum(
        (activity.end - activity.start).total_seconds() for activity in itinerary.activities
    )
    return min(1.0, used / span)


class LazyRetriever:
    """Builds the hybrid retriever on first use.

    Ingesting the corpus and encoding passages costs seconds; a planning
    conversation never routes to retrieval, so paying that at process start
    would slow every turn for a tool most turns do not use.
    """

    def __init__(self, store_kind: str) -> None:
        self.store_kind = store_kind
        self._retriever: Retriever | None = None

    def _build(self) -> Retriever:
        from app.rag.ingest import ingest_corpus
        from app.rag.retriever import HybridRetriever, dense_encoder_from_env
        from app.rag.store import create_vector_store

        return HybridRetriever(
            ingest_corpus(),
            dense_encoder_from_env(),
            store=create_vector_store(self.store_kind),
        )

    async def search(self, query: str, *, limit: int = 5):
        if self._retriever is None:
            self._retriever = self._build()
        return await self._retriever.search(query, limit=limit)


def build_pipeline(
    settings: Settings,
    *,
    retriever: Retriever | None = None,
    understand_call_name: str = "understand",
    narrate_call_name: str = "narrate",
    fixture_store=None,
    narrate_fixture_store=None,
    live_call_budget=None,
    narrate_settings: Settings | None = None,
) -> ConversationPipeline:
    """Wire the real components together for the endpoint and the CLI."""
    from app.llm.record_replay import FixtureStore
    from app.tools.opening_hours import OpeningHoursEngine
    from app.tools.travel import PrecomputedTravelTimeProvider
    from app.tools.weather import build_weather_provider

    repository = CatalogRepository()
    default_root = FixtureStore(
        DEFAULT_CONVERSATION_FIXTURE_ROOT
    ) if fixture_store is None else fixture_store
    return ConversationPipeline(
        settings=settings,
        repository=repository,
        hours=OpeningHoursEngine(repository),
        travel=PrecomputedTravelTimeProvider(),
        weather=build_weather_provider(settings),
        understander=Understander.from_settings(
            settings,
            repository.catalog,
            call_name=understand_call_name,
            fixture_store=default_root,
            live_call_budget=live_call_budget,
        ),
        narrator=Narrator.from_settings(
            narrate_settings or settings,
            repository.catalog,
            call_name=narrate_call_name,
            fixture_store=narrate_fixture_store or default_root,
            live_call_budget=live_call_budget,
        ),
        retriever=retriever or LazyRetriever(settings.rag_store),
    )
