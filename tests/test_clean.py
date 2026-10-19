"""Unit tests for src/clean.py data cleaning and session mapping.

Written FIRST per specification:
- DST spring-forward and fall-back weekends
- Sunday 18:00 ET open mapping to Monday's trade_date
- 17:00-18:00 ET daily break producing no rows in session output
- Boundary ticks at exactly 02:00:00 and 08:20:00 ET
- Sorting by (ts_event, sequence), keeping action 'T' with size > 0
- sgn (+1 for 'A', -1 for 'B', 0 for 'N') and sv (sgn * size)
- Flagging jumps over 10 ATR (without dropping) and counting side=='N' volume
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq

from src.clean import clean_trades_dataframe, load_and_clean_trades
from src.config import compute_trade_date, get_session_name

TZ_ET = ZoneInfo("America/New_York")


def test_dst_spring_forward_weekend() -> None:
    """Verify session mapping and trade_date across the US Spring-Forward DST transition.

    On Sunday, March 10, 2024, clocks jump from 02:00 EST to 03:00 EDT (UTC-5 to UTC-4).
    """
    # 1. Sunday 18:00 ET (22:00 UTC) -> Asia session, trade_date is Monday March 11
    dt_sun_open = datetime(2024, 3, 10, 18, 0, 0, tzinfo=TZ_ET)
    dt_sun_utc = dt_sun_open.astimezone(timezone.utc)
    assert dt_sun_utc.hour == 22  # EDT is UTC-4
    assert get_session_name(dt_sun_utc) == "asia"
    assert compute_trade_date(dt_sun_utc).isoformat() == "2024-03-11"

    # 2. Monday 02:00 ET (06:00 UTC) -> London session
    dt_mon_london = datetime(2024, 3, 11, 2, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(dt_mon_london) == "london"
    assert compute_trade_date(dt_mon_london).isoformat() == "2024-03-11"

    # 3. Monday 08:20 ET (12:20 UTC) -> NY session
    dt_mon_ny = datetime(2024, 3, 11, 8, 20, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(dt_mon_ny) == "ny"
    assert compute_trade_date(dt_mon_ny).isoformat() == "2024-03-11"


def test_dst_fall_back_weekend() -> None:
    """Verify session mapping and trade_date across the US Fall-Back DST transition.

    On Sunday, November 3, 2024, clocks fall back from 02:00 EDT to 01:00 EST (UTC-4 to UTC-5).
    """
    # 1. Sunday 18:00 ET (23:00 UTC) -> Asia session, trade_date is Monday November 4
    dt_sun_open = datetime(2024, 11, 3, 18, 0, 0, tzinfo=TZ_ET)
    dt_sun_utc = dt_sun_open.astimezone(timezone.utc)
    assert dt_sun_utc.hour == 23  # EST is UTC-5
    assert get_session_name(dt_sun_utc) == "asia"
    assert compute_trade_date(dt_sun_utc).isoformat() == "2024-11-04"

    # 2. Monday 02:00 ET (07:00 UTC) -> London session
    dt_mon_london = datetime(2024, 11, 4, 2, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(dt_mon_london) == "london"
    assert compute_trade_date(dt_mon_london).isoformat() == "2024-11-04"

    # 3. Monday 08:20 ET (13:20 UTC) -> NY session
    dt_mon_ny = datetime(2024, 11, 4, 8, 20, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(dt_mon_ny) == "ny"
    assert compute_trade_date(dt_mon_ny).isoformat() == "2024-11-04"


def test_sunday_1800_et_open_mapping() -> None:
    """Verify Sunday 18:00 ET open maps to Monday's trade_date and Asia session."""
    # Pre-open Sunday 17:59:59 ET -> break, not Monday
    t_pre = datetime(2026, 10, 18, 17, 59, 59, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_pre) is None
    assert compute_trade_date(t_pre).isoformat() == "2026-10-18"

    # Open exactly at 18:00:00 ET -> Asia, trade_date is Monday 2026-10-19
    t_open = datetime(2026, 10, 18, 18, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_open) == "asia"
    assert compute_trade_date(t_open).isoformat() == "2026-10-19"


def test_1700_to_1800_et_break_produces_no_rows() -> None:
    """Verify ticks occurring during the 17:00-18:00 ET daily break produce no session rows."""
    t_break_1 = datetime(2026, 10, 19, 17, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    t_break_2 = datetime(2026, 10, 19, 17, 30, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    t_break_3 = datetime(2026, 10, 19, 17, 59, 59, tzinfo=TZ_ET).astimezone(timezone.utc)

    for t in (t_break_1, t_break_2, t_break_3):
        assert get_session_name(t) is None

    # In clean pipeline, rows during break have sub=None and are excluded from active session output
    df_raw = pd.DataFrame(
        {
            "ts_event": [
                int(t_break_1.timestamp() * 1e9),
                int(t_break_2.timestamp() * 1e9),
            ],
            "sequence": [1, 2],
            "action": ["T", "T"],
            "size": [1, 2],
            "price": [2650.0, 2650.1],
            "side": ["A", "B"],
        }
    )
    cleaned_df, _ = clean_trades_dataframe(df_raw, drop_out_of_session=True)
    assert len(cleaned_df) == 0


def test_boundary_ticks_at_0200_and_0820_et() -> None:
    """Verify half-open interval boundaries:
    [18:00, 02:00) Asia, [02:00, 08:20) London, [08:20, 17:00) NY.
    """
    # 1 tick before 02:00 ET -> Asia
    t_pre_london = datetime(2026, 10, 20, 1, 59, 59, 999999, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_pre_london) == "asia"

    # Exactly 02:00:00.000000000 ET -> London
    t_at_london = datetime(2026, 10, 20, 2, 0, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_at_london) == "london"

    # 1 tick before 08:20 ET -> London
    t_pre_ny = datetime(2026, 10, 20, 8, 19, 59, 999999, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_pre_ny) == "london"

    # Exactly 08:20:00.000000000 ET -> NY
    t_at_ny = datetime(2026, 10, 20, 8, 20, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_at_ny) == "ny"

    # Exactly 17:00:00.000000000 ET -> Break (None)
    t_at_close = datetime(2026, 10, 20, 17, 0, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert get_session_name(t_at_close) is None


def test_clean_trades_filtering_sorting_and_signs() -> None:
    """Verify sorting by (ts_event, sequence), keeping action 'T' with size>0, sgn and sv."""
    t_ny = datetime(2026, 10, 20, 10, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    base_ts = int(t_ny.timestamp() * 1e9)

    df_raw = pd.DataFrame(
        {
            "ts_event": [base_ts + 200, base_ts + 100, base_ts + 100, base_ts + 300, base_ts + 400],
            "sequence": [1, 2, 1, 1, 1],
            "action": ["T", "T", "T", "C", "T"],  # Row 3 is 'C' (cancel/non-trade) -> should drop
            "size": [5, 2, 3, 10, 0],  # Row 4 has size=0 -> should drop
            "price": [2650.1, 2650.0, 2650.0, 2650.5, 2650.2],
            "side": ["A", "B", "N", "A", "B"],  # 'A'=+1, 'B'=-1, 'N'=0
        }
    )

    cleaned_df, stats = clean_trades_dataframe(df_raw, drop_out_of_session=False)

    # Expected retained rows: 3 valid 'T' trades with size > 0
    assert len(cleaned_df) == 3

    # Check sort order: (base_ts+100, seq 1), (base_ts+100, seq 2), (base_ts+200, seq 1)
    assert list(cleaned_df["ts_event"]) == [base_ts + 100, base_ts + 100, base_ts + 200]
    assert list(cleaned_df["sequence"]) == [1, 2, 1]

    # Check sgn: N -> 0, B -> -1, A -> +1
    assert list(cleaned_df["sgn"]) == [0, -1, 1]

    # Check sv (signed volume = sgn * size):
    # row 0: 0 * 3 = 0
    # row 1: -1 * 2 = -2
    # row 2: 1 * 5 = 5
    assert list(cleaned_df["sv"]) == [0, -2, 5]

    # Check counting side == 'N' by volume:
    assert stats["neutral_volume"] == 3


def test_jump_flagging_over_10_atr() -> None:
    """Verify price jumps over 10 ATR are flagged (is_jump=True) without being dropped."""
    t_ny = datetime(2026, 10, 20, 11, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    base_ts = int(t_ny.timestamp() * 1e9)

    # Generate 30 stable trades with 0.10 tick movement (ATR ~ 0.10)
    prices = [2650.0 + (i % 2) * 0.10 for i in range(30)]
    # Introduce an extreme jump of 5.00 points (> 10 * 0.10 = 1.00 ATR) at trade 31
    prices.append(2655.0)
    # Next trade returns or continues
    prices.append(2655.1)

    n = len(prices)
    df_raw = pd.DataFrame(
        {
            "ts_event": [base_ts + i * 1_000_000 for i in range(n)],
            "sequence": list(range(n)),
            "action": ["T"] * n,
            "size": [1] * n,
            "price": prices,
            "side": ["A"] * n,
        }
    )

    cleaned_df, stats = clean_trades_dataframe(df_raw, atr_window=10)

    assert len(cleaned_df) == n  # Not dropped!
    assert "is_jump" in cleaned_df.columns

    # Trade 30 is the jump to 2655.0
    jump_row = cleaned_df.iloc[30]
    assert jump_row["is_jump"] is True or jump_row["is_jump"] == 1
    assert stats["jump_count"] >= 1


def test_load_and_clean_trades_duckdb(tmp_path: Path) -> None:
    """Verify DuckDB loading, sorting, and parquet output."""
    raw_dir = tmp_path / "raw" / "gc_trades" / "date=2026-10-20"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / "part.parquet"

    t_ny = datetime(2026, 10, 20, 12, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    base_ts = int(t_ny.timestamp() * 1e9)

    sample_df = pd.DataFrame(
        {
            "ts_event": [base_ts + 20, base_ts + 10],
            "sequence": [1, 1],
            "action": ["T", "T"],
            "size": [2, 3],
            "price": [2651.0, 2651.1],
            "side": ["A", "B"],
        }
    )
    sample_df.to_parquet(raw_file, compression="zstd")

    out_file = tmp_path / "clean_trades.parquet"
    cleaned_path, stats = load_and_clean_trades(
        raw_input_path=raw_file,
        output_path=out_file,
    )

    assert cleaned_path.exists()
    tbl = pq.read_table(cleaned_path)
    assert tbl.num_rows == 2
    assert "trade_date" in tbl.column_names
    assert "sub" in tbl.column_names
    assert "sgn" in tbl.column_names
    assert "sv" in tbl.column_names
    assert "is_jump" in tbl.column_names
