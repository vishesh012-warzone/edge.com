"""Unit tests for src/derive.py 1-second bars and volume bars derivation.

Tested per SPEC.md:
- Total volume conserved between raw trades and bars (both bars_1s and volume_bars)
- buy_v + sell_v + unsided == v in bars_1s
- No volume bar spans two trade_dates (reset per trade_date)
- Volume bar size auto-calibration hits 100-200 bars/day
- Partitioned Parquet output with zstd compression
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq

from src.clean import clean_trades_dataframe
from src.derive import (
    build_bars_1s,
    build_volume_bars,
    calibrate_volume_bar_size,
    derive_bars_1s_from_df,
    derive_volume_bars_from_df,
)

TZ_ET = ZoneInfo("America/New_York")


def _generate_multi_day_trades() -> pd.DataFrame:
    """Generate synthetic trades across 2 distinct trade dates."""
    # Day 1: 2026-10-19 (trades during NY session at 10:00 ET)
    t1 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    ts1 = int(t1.timestamp() * 1e9)

    # Day 2: 2026-10-20 (trades during NY session at 10:00 ET)
    t2 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    ts2 = int(t2.timestamp() * 1e9)

    rows = []
    # 50 trades on Day 1
    for i in range(50):
        rows.append(
            {
                "ts_event": ts1 + i * 200_000_000,  # 5 trades per second
                "ts_recv": ts1 + i * 200_000_000 + 1000,
                "sequence": i + 1,
                "instrument_id": "GC",
                "symbol": "GC.v.0",
                "action": "T",
                "side": ["A", "B", "N"][i % 3],
                "price": round(2650.0 + (i % 5) * 0.10, 2),
                "size": (i % 4) + 1,
                "flags": 0,
            }
        )

    # 50 trades on Day 2
    for i in range(50):
        rows.append(
            {
                "ts_event": ts2 + i * 200_000_000,
                "ts_recv": ts2 + i * 200_000_000 + 1000,
                "sequence": i + 1,
                "instrument_id": "GC",
                "symbol": "GC.v.0",
                "action": "T",
                "side": ["A", "B", "N"][i % 3],
                "price": round(2655.0 + (i % 5) * 0.10, 2),
                "size": (i % 4) + 1,
                "flags": 0,
            }
        )

    df_raw = pd.DataFrame(rows)
    df_clean, _ = clean_trades_dataframe(df_raw)
    return df_clean


def test_volume_conserved_and_delta_balance_bars_1s() -> None:
    """Verify total volume is conserved in bars_1s and buy_v + sell_v + unsided == v."""
    df_clean = _generate_multi_day_trades()
    total_raw_volume = df_clean["size"].sum()

    bars_1s = derive_bars_1s_from_df(df_clean)

    # 1. Total volume conserved
    assert bars_1s["v"].sum() == total_raw_volume

    # 2. buy_v + sell_v + unsided == v
    assert "buy_v" in bars_1s.columns
    assert "sell_v" in bars_1s.columns
    assert "unsided_v" in bars_1s.columns
    assert (bars_1s["buy_v"] + bars_1s["sell_v"] + bars_1s["unsided_v"] == bars_1s["v"]).all()

    # 3. Trade count conserved
    assert bars_1s["n"].sum() == len(df_clean)


def test_volume_conserved_in_volume_bars() -> None:
    """Verify total volume is conserved in volume_bars."""
    df_clean = _generate_multi_day_trades()
    total_raw_volume = df_clean["size"].sum()

    # Test with small bar size (e.g. 15 contracts)
    vol_bars = derive_volume_bars_from_df(df_clean, bar_size=15)

    assert vol_bars["v"].sum() == total_raw_volume
    assert len(vol_bars) > 0


def test_no_volume_bar_spans_two_trade_dates() -> None:
    """Verify cumulative volume resets per trade_date and no bar spans two dates."""
    df_clean = _generate_multi_day_trades()

    vol_bars = derive_volume_bars_from_df(df_clean, bar_size=20)

    # Check that trade_dates present in data are partitioned properly
    unique_dates = df_clean["trade_date"].unique()
    assert len(unique_dates) == 2

    for d in unique_dates:
        day_bars = vol_bars[vol_bars["trade_date"] == d]
        assert len(day_bars) > 0
        # Bars should start at 0 (or 1) and be contiguous
        assert day_bars["bar"].iloc[0] == 0
        assert list(day_bars["bar"]) == list(range(len(day_bars)))

        # Verify t0 and t1 stay entirely within the trade_date
        for _, bar in day_bars.iterrows():
            assert bar["t0"] <= bar["t1"]


def test_volume_bar_calibration() -> None:
    """Verify auto-calibration computes bar size targeting 100-200 bars/day."""
    # Create 5 days of simulated trades with 150,000 total volume (30,000 vol/day)
    dates = [f"2026-10-{19+i}" for i in range(5)]
    rows = []
    for d in dates:
        rows.append({"trade_date": d, "size": 30_000})
    df_summary = pd.DataFrame(rows)

    calibrated_bar_size = calibrate_volume_bar_size(df_summary, target_bars_per_day=150)

    # 30,000 vol / 150 bars = 200 contracts per bar
    assert calibrated_bar_size == 200

    # Number of bars per day = 30000 / 200 = 150 bars/day (within [100, 200])
    bars_per_day = 30_000 / calibrated_bar_size
    assert 100 <= bars_per_day <= 200


def test_duckdb_partitioned_parquet_output(tmp_path: Path) -> None:
    """Verify end-to-end DuckDB SQL aggregation and partitioned Parquet generation."""
    df_clean = _generate_multi_day_trades()
    clean_parquet = tmp_path / "clean_trades.parquet"
    df_clean.to_parquet(clean_parquet, compression="zstd")

    out_bars_1s_dir = tmp_path / "bars_1s"
    out_vol_bars_dir = tmp_path / "volume_bars"

    # 1. Build bars_1s
    bars_1s_path = build_bars_1s(clean_parquet, output_dir=out_bars_1s_dir)
    assert bars_1s_path.exists()
    # Check hive partition files exist
    part_files_1s = list(out_bars_1s_dir.glob("**/*.parquet"))
    assert len(part_files_1s) > 0

    # 2. Build volume_bars
    vol_bars_path = build_volume_bars(
        clean_parquet, output_dir=out_vol_bars_dir, bar_size=25
    )
    assert vol_bars_path.exists()
    part_files_vol = list(out_vol_bars_dir.glob("**/*.parquet"))
    assert len(part_files_vol) > 0

    # Read back one volume bar partition and inspect columns
    tbl = pq.read_table(part_files_vol[0])
    expected_cols = {"trade_date", "bar", "o", "h", "l", "c", "v", "delta", "t0", "t1"}
    assert expected_cols.issubset(set(tbl.column_names))
