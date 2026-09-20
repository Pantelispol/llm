"""Deterministic resolution of relative dates and durations.

The model may quote the traveler's wording; it never decides a date, a
timezone, or a clock time. Everything here is computed from the phrase plus the
server-injected `now`, normalized to the catalog timezone.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.models import TimeWindow

ATHENS = ZoneInfo("Europe/Athens")
MAX_WINDOW_HOURS = 14

DAY_OFFSETS: dict[str, int] = {
    "today": 0,
    "tonight": 0,
    "this afternoon": 0,
    "this morning": 0,
    "σημερα": 0,
    "αποψε": 0,
    "tomorrow": 1,
    "αυριο": 1,
    "the day after tomorrow": 2,
    "day after tomorrow": 2,
    "μεθαυριο": 2,
}
NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "μια": 1,
    "μία": 1,
    "δυο": 2,
    "δύο": 2,
    "τρεις": 3,
    "τεσσερις": 4,
    "πεντε": 5,
    "εξι": 6,
    "επτα": 7,
    "εφτα": 7,
    "οκτω": 8,
    "οχτω": 8,
    "εννεα": 9,
    "δεκα": 10,
}
HOUR_WORDS = ("hours", "hour", "ωρες", "ωρα", "ωρων")


def normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", without_marks, flags=re.UNICODE))


# Greek casefolding maps final sigma to sigma, so the lookup tables are
# normalized once here rather than compared against raw source spellings.
NORMALIZED_NUMBER_WORDS = {normalize(word): value for word, value in NUMBER_WORDS.items()}
NORMALIZED_HOUR_WORDS = frozenset(normalize(word) for word in HOUR_WORDS)


def to_athens(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ATHENS)


def resolve_date(text: str | None, now: datetime) -> date | None:
    """Resolve a relative date phrase against an injected, Athens-normalized now."""
    if not text:
        return None
    normalized = f" {normalize(text)} "
    for phrase, offset in sorted(DAY_OFFSETS.items(), key=lambda item: -len(item[0])):
        if f" {normalize(phrase)} " in normalized:
            return (to_athens(now) + timedelta(days=offset)).date()
    return None


def extract_duration_hours(user_turn: str) -> int | None:
    """Pull a whole-hour duration such as 'five hours' or '5 ωρες' from the turn."""
    normalized = normalize(user_turn)
    tokens = normalized.split()
    for index, token in enumerate(tokens[:-1]):
        if tokens[index + 1] not in NORMALIZED_HOUR_WORDS:
            continue
        if token.isdigit():
            hours = int(token)
        elif token in NORMALIZED_NUMBER_WORDS:
            hours = NORMALIZED_NUMBER_WORDS[token]
        else:
            continue
        if 1 <= hours <= MAX_WINDOW_HOURS:
            return hours
    return None


def resolve_time_window(
    user_turn: str,
    requested_date_text: str | None,
    now: datetime,
    *,
    day_start_hour: int,
) -> tuple[TimeWindow | None, list[str]]:
    """Build a window from a relative date and a duration, recording assumptions."""
    day = resolve_date(requested_date_text, now) or resolve_date(user_turn, now)
    hours = extract_duration_hours(user_turn)
    if day is None and hours is None:
        return None, []

    assumptions: list[str] = []
    athens_now = to_athens(now)
    if day is None:
        day = athens_now.date()
        assumptions.append("No date was given, so today was assumed.")

    start = datetime.combine(day, time(hour=day_start_hour), tzinfo=ATHENS)
    if day == athens_now.date() and athens_now > start:
        start = athens_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if hours is None:
        hours = MAX_WINDOW_HOURS // 2
        assumptions.append(f"No duration was given, so {hours} hours were assumed.")
    else:
        assumptions.append(
            f"A {hours}-hour visit on {day.isoformat()} was assumed to begin at "
            f"{start:%H:%M} local time."
        )
    return TimeWindow(start=start, end=start + timedelta(hours=hours)), assumptions


CLOCK_PATTERN = re.compile(
    r"(?<![\d:.])(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)\s*(?P<meridiem>[ap]\.?m\.?)?",
    flags=re.IGNORECASE,
)
_CLOSING_WORDS = frozenset(
    {"close", "closes", "closing", "closed", "κλεινει", "κλεισιμο", "κλειστο"}
)
_END_OF_DAY = time(hour=23, minute=59)


def _clock_value(match: re.Match[str]) -> time | None:
    hour, minute = int(match["hour"]), int(match["minute"])
    meridiem = (match["meridiem"] or "").lower().replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return time(hour=hour, minute=minute)


def extract_clock_times(user_turn: str) -> list[time]:
    """Clock times the traveler stated as their own, excluding closing-time claims.

    "Arrive at 16:45 and it closes at 17:00" states one arrival; 17:00 is a
    premise about the venue. The opening-hours engine is the authority on that,
    so it must not be read as the end of the traveler's window.
    """
    times: list[time] = []
    for match in CLOCK_PATTERN.finditer(user_turn):
        preceding = normalize(user_turn[: match.start()]).split()[-3:]
        if _CLOSING_WORDS.intersection(preceding):
            continue
        value = _clock_value(match)
        if value is not None:
            times.append(value)
    return times


def resolve_feasibility_window(
    user_turn: str,
    requested_date_text: str | None,
    now: datetime,
) -> tuple[TimeWindow | None, list[str]]:
    """The traveler's hypothesis as a window: an arrival time or an explicit range.

    Returns None when the turn states no clock time, so the caller can fall back
    to the stored window or ask, rather than inventing one.
    """
    times = extract_clock_times(user_turn)
    if not times:
        return None, []
    athens_now = to_athens(now)
    day = resolve_date(requested_date_text, now) or resolve_date(user_turn, now)
    assumptions: list[str] = []
    if day is None:
        day = athens_now.date()
        assumptions.append("No date was given, so today was assumed.")
    start = datetime.combine(day, times[0], tzinfo=ATHENS)
    if len(times) >= 2 and times[1] > times[0]:
        end = datetime.combine(day, times[1], tzinfo=ATHENS)
    else:
        end = datetime.combine(day, _END_OF_DAY, tzinfo=ATHENS)
        assumptions.append(
            f"You gave an arrival time of {times[0]:%H:%M} and no end time, so the "
            "rest of the day was treated as available."
        )
    return TimeWindow(start=start, end=end), assumptions
