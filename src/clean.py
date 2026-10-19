"""Data cleaning and aggregation module.

Converts raw tick/trade feeds into Parquet tables:
- bars_1s: ts, instrument_id, o, h, l, c, v, buy_v, sell_v, n
- volume_bars: trade_date, bar, o, h, l, c, v, delta, t0, t1
All timestamps stored UTC (ns). Volume bars target 100-200 bars/day.
"""

from __future__ import annotations

from pathlib import Path

from src.config import PROCESSED_DATA_DIR


def clean_gc_trades(raw_input_path: Path, output_path: Path) -> Path:
    """Normalize raw Databento CME GC trades, ensuring strictly increasing UTC ns timestamps."""
    raise NotImplementedError("Clean GC trades stub")


def build_bars_1s(
    clean_trades_path: Path,
    output_path: Path = PROCESSED_DATA_DIR / "bars_1s",
) -> Path:
    """Aggregate trade data into 1-second OHLCV bars with buy/sell volume and trade counts."""
    raise NotImplementedError("Build 1s bars stub")


def build_volume_bars(
    clean_trades_path: Path,
    output_path: Path = PROCESSED_DATA_DIR / "volume_bars",
    target_bars_per_day: int = 150,
) -> Path:
    """Construct dynamic volume bars targeting 100-200 bars/day with delta calculation."""
    raise NotImplementedError("Build volume bars stub")
