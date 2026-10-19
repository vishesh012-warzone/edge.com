"""Session and Volume Profile feature computation.

Builds Parquet table:
- sessions: trade_date, sub, instrument_id, o, h, l, c, vol, buy_v, sell_v,
            n_trades, poc, val, vah, t_start, t_end

Hard Rules:
- NO LOOK-AHEAD: A session's profile/levels may only be used by sessions
  that start after it ends. Features computed from completed data only.
- Value area = 70% of total session volume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import PROCESSED_DATA_DIR, VALUE_AREA_PCT


def compute_volume_profile(
    prices: Any,
    volumes: Any,
    tick_size: float = 0.10,
    value_area_pct: float = VALUE_AREA_PCT,
) -> dict[str, float]:
    """Calculate Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL).

    Args:
        prices: Array-like of trade/bar prices.
        volumes: Array-like of trade/bar volumes.
        tick_size: Price discretization unit (GC = 0.10).
        value_area_pct: Target volume fraction for value area (default 0.70).

    Returns:
        Dict with keys: 'poc', 'vah', 'val'.
    """
    raise NotImplementedError("Volume profile calculation stub")


def build_sessions_table(
    volume_bars_path: Path,
    output_path: Path = PROCESSED_DATA_DIR / "sessions",
) -> Path:
    """Build the sessions Parquet table partitioned by trade_date."""
    raise NotImplementedError("Build sessions table stub")
