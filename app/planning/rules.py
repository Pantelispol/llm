from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.domain.models import Pace, TripState
from app.domain.ports import HourlyWeatherFlags, PaceFactors, PlanningContext, TravelLeg


def pace_factors_for(state: TripState) -> PaceFactors:
    travel = 1.25 if state.pace == Pace.RELAXED else 1.0
    visit = 1.25 if state.pace == Pace.RELAXED else 1.0
    if state.party.children_ages:
        travel *= 1.3
        visit *= 1.15
    return PaceFactors(
        travel_time_multiplier=travel,
        visit_time_multiplier=visit,
    )


def adjusted_minutes(minutes: int, multiplier: float) -> int:
    return math.ceil(minutes * multiplier)


def travel_leg(context: PlanningContext, origin_id: str, destination_id: str) -> TravelLeg:
    try:
        return next(
            leg
            for leg in context.travel_matrix.legs
            if leg.origin_id == origin_id and leg.destination_id == destination_id
        )
    except StopIteration as error:
        raise ValueError(f"travel matrix is missing {origin_id} -> {destination_id}") from error


def weather_during(
    context: PlanningContext,
    start: datetime,
    end: datetime,
) -> list[HourlyWeatherFlags]:
    one_hour = timedelta(hours=1)
    return [
        flags
        for flags in context.hourly_weather_flags
        if flags.at < end and flags.at + one_hour > start
    ]
