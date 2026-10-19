"""Configuration and session boundary specifications.

Governed by docs/SPEC.md and AGENTS.md.
All timestamps are stored UTC (ns).
Session logic uses America/New_York wall-clock via this module ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Global Constants & Seeds ---
DEFAULT_SEED: int = 0
TICK_SIZE_GC: float = 0.10
VALUE_AREA_PCT: float = 0.70
TARGET_VOLUME_BARS_PER_DAY_MIN: int = 100
TARGET_VOLUME_BARS_PER_DAY_MAX: int = 200

# --- Timezones ---
TZ_UTC: ZoneInfo = ZoneInfo("UTC")
TZ_NEW_YORK: ZoneInfo = ZoneInfo("America/New_York")

# --- Directory & File Paths ---
BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
RAW_DATA_DIR: Path = DATA_DIR / "raw"
PROCESSED_DATA_DIR: Path = DATA_DIR / "processed"
TRIALS_PATH: Path = DATA_DIR / "trials.jsonl"


# --- Session Definitions (America/New_York Wall-Clock) ---
@dataclass(frozen=True)
class SessionWindow:
    """Intraday session time interval in America/New_York wall-clock."""

    name: str
    start_time: time
    end_time: time
    spans_midnight: bool = False


# Sessions (ET wall-clock):
# - asia: 18:00-02:00 (spans midnight)
# - london: 02:00-08:20
# - ny: 08:20-17:00
SESSIONS: dict[str, SessionWindow] = {
    "asia": SessionWindow(
        name="asia",
        start_time=time(18, 0),
        end_time=time(2, 0),
        spans_midnight=True,
    ),
    "london": SessionWindow(
        name="london",
        start_time=time(2, 0),
        end_time=time(8, 20),
        spans_midnight=False,
    ),
    "ny": SessionWindow(
        name="ny",
        start_time=time(8, 20),
        end_time=time(17, 0),
        spans_midnight=False,
    ),
}


def to_new_york_datetime(dt_or_ns: datetime | int) -> datetime:
    """Convert a UTC timestamp (ns integer or datetime) to America/New_York datetime."""
    if isinstance(dt_or_ns, int):
        dt_utc = datetime.fromtimestamp(dt_or_ns / 1e9, tz=TZ_UTC)
    elif dt_or_ns.tzinfo is None:
        dt_utc = dt_or_ns.replace(tzinfo=TZ_UTC)
    else:
        dt_utc = dt_or_ns.astimezone(TZ_UTC)
    return dt_utc.astimezone(TZ_NEW_YORK)


def compute_trade_date(dt_or_ns: datetime | int) -> date:
    """Compute the trade_date defined as the calendar date of (ET + 6h).

    Per docs/SPEC.md: trade_date = date of (ET + 6h).
    """
    dt_ny = to_new_york_datetime(dt_or_ns)
    shifted = dt_ny + timedelta(hours=6)
    return shifted.date()


def get_session_name(dt_or_ns: datetime | int) -> str | None:
    """Determine the session (asia, london, ny) for a given UTC timestamp.

    Returns the session name or None if outside predefined windows.
    """
    dt_ny = to_new_york_datetime(dt_or_ns)
    t = dt_ny.time()

    # Check non-midnight spanning sessions first
    for name, s in SESSIONS.items():
        if not s.spans_midnight:
            if s.start_time <= t < s.end_time:
                return name

    # Check sessions spanning midnight (e.g. asia: 18:00 - 02:00)
    for name, s in SESSIONS.items():
        if s.spans_midnight:
            if t >= s.start_time or t < s.end_time:
                return name

    return None
