"""Tests for session configuration and trade date calculation."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.config import (
    DEFAULT_SEED,
    TICK_SIZE_GC,
    VALUE_AREA_PCT,
    compute_trade_date,
    get_session_name,
)


def test_constants() -> None:
    """Verify core specification constants from docs/SPEC.md."""
    assert TICK_SIZE_GC == 0.10
    assert VALUE_AREA_PCT == 0.70
    assert DEFAULT_SEED == 0


def test_trade_date_calculation() -> None:
    """Verify trade_date equals date of (ET + 6h)."""
    tz_et = ZoneInfo("America/New_York")

    # Example 1: 2026-10-19 18:30 ET (Asia session start of trade date 2026-10-20)
    # 18:30 ET + 6h = 00:30 next day -> trade_date is 2026-10-20
    dt_et = datetime(2026, 10, 19, 18, 30, tzinfo=tz_et)
    dt_utc = dt_et.astimezone(timezone.utc)
    assert compute_trade_date(dt_utc).isoformat() == "2026-10-20"

    # Example 2: 2026-10-20 09:30 ET (NY session on trade date 2026-10-20)
    # 09:30 ET + 6h = 15:30 same day -> trade_date is 2026-10-20
    dt_et = datetime(2026, 10, 20, 9, 30, tzinfo=tz_et)
    dt_utc = dt_et.astimezone(timezone.utc)
    assert compute_trade_date(dt_utc).isoformat() == "2026-10-20"


def test_session_mapping() -> None:
    """Verify sessions match ET wall-clock: asia 18:00-02:00, london 02:00-08:20, ny 08:20-17:00."""
    tz_et = ZoneInfo("America/New_York")

    # Asia session: 20:00 ET
    t_asia = datetime(2026, 10, 19, 20, 0, tzinfo=tz_et)
    assert get_session_name(t_asia) == "asia"

    # Asia session past midnight: 01:00 ET
    t_asia_post = datetime(2026, 10, 20, 1, 0, tzinfo=tz_et)
    assert get_session_name(t_asia_post) == "asia"

    # London session: 04:00 ET
    t_london = datetime(2026, 10, 20, 4, 0, tzinfo=tz_et)
    assert get_session_name(t_london) == "london"

    # NY session: 10:00 ET
    t_ny = datetime(2026, 10, 20, 10, 0, tzinfo=tz_et)
    assert get_session_name(t_ny) == "ny"

    # Between sessions or post close: 17:30 ET
    t_closed = datetime(2026, 10, 20, 17, 30, tzinfo=tz_et)
    assert get_session_name(t_closed) is None
