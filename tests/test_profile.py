"""Property and unit tests for src/profile.py: volume profile and sessions table."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.clean import clean_trades_dataframe
from src.profile import (
    SESSION_COLUMNS,
    build_sessions_table,
    compute_volume_profile,
    volume_profile,
)


def _generate_synthetic_trades_for_profile(
    rng: np.random.Generator,
    n_trades: int = 1000,
    base_price: float = 2000.0,
    price_spread: float = 10.0,
    tick: float = 0.10,
) -> tuple[np.ndarray, np.ndarray]:
    """Helper to generate random price and size series aligned to tick."""
    ticks_range = int(round(price_spread / tick))
    tick_offsets = rng.integers(-ticks_range, ticks_range + 1, size=n_trades)
    prices = np.round(base_price + tick_offsets * tick, 4)
    sizes = rng.integers(1, 50, size=n_trades)
    return prices, sizes


def test_volume_profile_invariants_val_poc_vah():
    """Property test: VAL <= POC <= VAH across multiple random price distributions."""
    rng = np.random.default_rng(101)
    for _ in range(20):
        prices, sizes = _generate_synthetic_trades_for_profile(rng, n_trades=300)
        poc, val, vah, prof = volume_profile(prices, sizes, tick=0.10, va=0.70)
        assert val <= poc, f"Invariant violated: VAL ({val}) > POC ({poc})"
        assert poc <= vah, f"Invariant violated: POC ({poc}) > VAH ({vah})"


def test_volume_profile_poc_inside_range():
    """Property test: POC inside [low, high]."""
    rng = np.random.default_rng(102)
    for _ in range(20):
        prices, sizes = _generate_synthetic_trades_for_profile(rng, n_trades=300)
        low = float(np.min(prices))
        high = float(np.max(prices))
        poc, val, vah, prof = volume_profile(prices, sizes, tick=0.10, va=0.70)
        assert low <= poc <= high, f"POC ({poc}) outside [{low}, {high}]"
        assert low <= val <= high, f"VAL ({val}) outside [{low}, {high}]"
        assert low <= vah <= high, f"VAH ({vah}) outside [{low}, {high}]"


def test_volume_profile_value_area_volume_pct():
    """Property test: value-area volume >= 70% of total volume."""
    rng = np.random.default_rng(103)
    tick = 0.10
    va_target_pct = 0.70
    for _ in range(20):
        prices, sizes = _generate_synthetic_trades_for_profile(rng, n_trades=400)
        poc, val, vah, prof = volume_profile(prices, sizes, tick=tick, va=va_target_pct)
        total_vol = float(np.sum(prof))

        min_p = float(np.min(prices))
        val_idx = int(round((val - min_p) / tick))
        vah_idx = int(round((vah - min_p) / tick))

        va_vol = float(np.sum(prof[val_idx : vah_idx + 1]))
        assert va_vol >= va_target_pct * total_vol - 1e-6, (
            f"VA volume ({va_vol}) < {va_target_pct} * total ({total_vol})"
        )


def test_volume_profile_result_invariant_to_row_order():
    """Property test: result is completely invariant to row order (permutations)."""
    rng = np.random.default_rng(104)
    prices, sizes = _generate_synthetic_trades_for_profile(rng, n_trades=600)

    poc_base, val_base, vah_base, prof_base = volume_profile(prices, sizes, tick=0.10, va=0.70)

    for _ in range(5):
        perm = rng.permutation(len(prices))
        prices_p = prices[perm]
        sizes_p = sizes[perm]

        poc_p, val_p, vah_p, prof_p = volume_profile(prices_p, sizes_p, tick=0.10, va=0.70)

        assert poc_p == poc_base, f"POC changed on permutation: {poc_p} != {poc_base}"
        assert val_p == val_base, f"VAL changed on permutation: {val_p} != {val_base}"
        assert vah_p == vah_base, f"VAH changed on permutation: {vah_p} != {vah_base}"
        np.testing.assert_array_equal(prof_p, prof_base, err_msg="Profile bincount changed")


def test_volume_profile_edge_cases():
    """Test single price, zero volume, and empty input edge cases."""
    # Single price
    prices = np.array([2000.5, 2000.5, 2000.5])
    sizes = np.array([10, 20, 30])
    poc, val, vah, prof = volume_profile(prices, sizes, tick=0.10, va=0.70)
    assert poc == 2000.5
    assert val == 2000.5
    assert vah == 2000.5
    assert float(np.sum(prof)) == 60.0

    # compute_volume_profile dict wrapper
    res_dict = compute_volume_profile(prices, sizes, tick_size=0.10, value_area_pct=0.70)
    assert res_dict["poc"] == 2000.5
    assert res_dict["val"] == 2000.5
    assert res_dict["vah"] == 2000.5

    # Empty data
    with pytest.raises(ValueError, match="empty data"):
        volume_profile([], [], tick=0.10, va=0.70)


def test_build_sessions_table(tmp_path):
    """Test sessions table construction from clean trades."""
    tz_et = ZoneInfo("America/New_York")
    t_asia = datetime(2026, 10, 18, 19, 0, 0, tzinfo=tz_et).astimezone(timezone.utc)
    t_london = datetime(2026, 10, 19, 4, 0, 0, tzinfo=tz_et).astimezone(timezone.utc)
    t_ny = datetime(2026, 10, 19, 10, 0, 0, tzinfo=tz_et).astimezone(timezone.utc)
    t_break = datetime(2026, 10, 19, 17, 30, 0, tzinfo=tz_et).astimezone(timezone.utc)

    ts_asia = int(t_asia.timestamp() * 1e9)
    ts_london = int(t_london.timestamp() * 1e9)
    ts_ny = int(t_ny.timestamp() * 1e9)
    ts_break = int(t_break.timestamp() * 1e9)

    raw_df = pd.DataFrame(
        {
            "ts_event": [
                ts_asia,
                ts_asia + 1_000_000,
                ts_london,
                ts_london + 1_000_000,
                ts_ny,
                ts_ny + 1_000_000,
                ts_break,
            ],
            "sequence": [1, 2, 1, 2, 1, 2, 1],
            "action": ["T", "T", "T", "T", "T", "T", "T"],
            "size": [5, 10, 15, 20, 25, 30, 8],
            "price": [2650.1, 2650.3, 2652.0, 2652.5, 2655.0, 2654.8, 2653.0],
            "side": ["A", "B", "A", "B", "A", "B", "A"],
            "instrument_id": [1001, 1001, 1001, 1001, 1001, 1001, 1001],
        }
    )

    clean_df, _ = clean_trades_dataframe(raw_df)

    output_dir = tmp_path / "sessions"
    res_path = build_sessions_table(clean_df, output_path=output_dir)

    assert res_path.exists()
    parquet_files = list(output_dir.glob("trade_date=*/part.parquet"))
    assert len(parquet_files) > 0

    sessions_df = pd.concat([pd.read_parquet(p) for p in parquet_files], ignore_index=True)

    # Check columns match docs/SPEC.md
    for col in SESSION_COLUMNS:
        assert col in sessions_df.columns, f"Missing column {col} in sessions table"

    # Maintenance break trade should be excluded (only asia, london, ny)
    assert set(sessions_df["sub"]) == {"asia", "london", "ny"}
    assert len(sessions_df) == 3

    # Check invariants on sessions table
    for _, row in sessions_df.iterrows():
        assert row["val"] <= row["poc"] <= row["vah"]
        assert row["l"] <= row["poc"] <= row["h"]
        assert row["vol"] > 0
        assert row["t_start"] <= row["t_end"]
        assert row["sub"] in {"asia", "london", "ny"}
