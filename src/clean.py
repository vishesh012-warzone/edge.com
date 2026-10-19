"""Data cleaning and normalization module for CME GC trades.

Governed by docs/SPEC.md and AGENTS.md:
- Loads raw trades using DuckDB over Parquet.
- Sorts by (ts_event, sequence) ascending.
- Filters: action == 'T' AND size > 0.
- Adds:
    - trade_date: calendar date of (ET + 6h)
    - sub: intraday session (asia, london, ny, or None during break)
    - sgn: signed trade direction (+1 for 'A', -1 for 'B', 0 for 'N')
    - sv: signed volume (sgn * size)
    - is_jump: boolean flag for price jumps > 10 ATR (without dropping rows)
- Computes statistics including neutral volume (side == 'N').
- Preserves raw immutability; outputs zstd Parquet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.config import (
    PROCESSED_DATA_DIR,
    TICK_SIZE_GC,
    compute_trade_date,
    get_session_name,
)

CLEAN_TRADES_COLUMNS: list[str] = [
    "ts_event",
    "ts_recv",
    "sequence",
    "instrument_id",
    "symbol",
    "action",
    "side",
    "price",
    "size",
    "flags",
    "trade_date",
    "sub",
    "sgn",
    "sv",
    "is_jump",
]


def clean_trades_dataframe(
    df: pd.DataFrame,
    drop_out_of_session: bool = False,
    atr_window: int = 50,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Clean, sort, and enrich trade DataFrame with session, delta, and jump flags.

    Args:
        df: Raw trades DataFrame.
        drop_out_of_session: If True, discards trades during breaks/closed market.
        atr_window: Window size for calculating rolling Average True Range.

    Returns:
        Tuple of (cleaned_df, stats_dict).
    """
    if df.empty:
        empty_df = pd.DataFrame(columns=CLEAN_TRADES_COLUMNS)
        return empty_df, {"total_volume": 0, "neutral_volume": 0, "jump_count": 0}

    # 1. Filter: keep action == 'T' and size > 0
    mask = (df["action"] == "T") & (df["size"] > 0)
    df_filtered = df[mask].copy()

    if df_filtered.empty:
        empty_df = pd.DataFrame(columns=CLEAN_TRADES_COLUMNS)
        return empty_df, {"total_volume": 0, "neutral_volume": 0, "jump_count": 0}

    # 2. Sort by (ts_event, sequence) ascending
    sort_cols = ["ts_event"]
    if "sequence" in df_filtered.columns:
        sort_cols.append("sequence")
    df_sorted = df_filtered.sort_values(by=sort_cols, ascending=True).reset_index(drop=True)

    # 3. Add trade_date: date of (ET + 6h)
    df_sorted["trade_date"] = [
        compute_trade_date(ts).isoformat() for ts in df_sorted["ts_event"]
    ]

    # 4. Add sub: session name (asia, london, ny, or None during break)
    df_sorted["sub"] = [get_session_name(ts) for ts in df_sorted["ts_event"]]

    if drop_out_of_session:
        df_sorted = df_sorted[df_sorted["sub"].notna()].reset_index(drop=True)

    if df_sorted.empty:
        empty_df = pd.DataFrame(columns=CLEAN_TRADES_COLUMNS)
        return empty_df, {"total_volume": 0, "neutral_volume": 0, "jump_count": 0}

    # 5. Add sgn: +1 for 'A' (ask/buyer aggressor), -1 for 'B' (bid/seller aggressor), 0 for 'N'
    side_series = df_sorted["side"].astype(str)
    sgn_map = {"A": 1, "B": -1, "N": 0}
    df_sorted["sgn"] = side_series.map(sgn_map).fillna(0).astype(int)

    # 6. Add sv: signed volume (sgn * size)
    df_sorted["sv"] = df_sorted["sgn"] * df_sorted["size"]

    # 7. Flag jumps > 10 ATR (do not drop)
    prices = df_sorted["price"].astype(float)
    delta_p = prices.diff().abs()
    rolling_tr = delta_p.rolling(window=atr_window, min_periods=1).mean()
    atr_baseline = rolling_tr.shift(1).fillna(rolling_tr).clip(lower=TICK_SIZE_GC)
    is_jump_mask = (delta_p > (10.0 * atr_baseline)).fillna(False)
    # First row cannot be a jump
    is_jump_mask.iloc[0] = False
    df_sorted["is_jump"] = is_jump_mask

    # 8. Compute statistics including side == 'N' by volume
    total_volume = int(df_sorted["size"].sum())
    neutral_volume = int(df_sorted.loc[side_series == "N", "size"].sum())
    jump_count = int(is_jump_mask.sum())

    stats = {
        "trade_count": len(df_sorted),
        "total_volume": total_volume,
        "neutral_volume": neutral_volume,
        "neutral_volume_pct": (neutral_volume / total_volume * 100.0) if total_volume > 0 else 0.0,
        "jump_count": jump_count,
    }

    return df_sorted, stats


def load_and_clean_trades(
    raw_input_path: Path,
    output_path: Path | None = None,
    drop_out_of_session: bool = False,
    atr_window: int = 50,
) -> tuple[Path, dict[str, Any]]:
    """Load raw trades via DuckDB, clean, sort, enrich, and write Parquet table.

    Args:
        raw_input_path: File or directory glob containing raw Parquet trades.
        output_path: Destination path for cleaned Parquet table.
        drop_out_of_session: If True, excludes rows outside asia/london/ny.
        atr_window: ATR lookback window for jump detection.

    Returns:
        Tuple of (output_file_path, stats_dict).
    """
    con = duckdb.connect(database=":memory:")
    path_str = str(raw_input_path).replace("\\", "/")

    query = f"""
        SELECT *
        FROM read_parquet('{path_str}')
        WHERE action = 'T' AND size > 0
        ORDER BY ts_event ASC, sequence ASC
    """
    df_raw = con.execute(query).df()
    con.close()

    df_cleaned, stats = clean_trades_dataframe(
        df_raw,
        drop_out_of_session=drop_out_of_session,
        atr_window=atr_window,
    )

    dest_path = output_path or (PROCESSED_DATA_DIR / "clean_trades.parquet")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(df_cleaned, preserve_index=False)
    pq.write_table(table, dest_path, compression="zstd")

    return dest_path, stats


def clean_gc_trades(raw_input_path: Path, output_path: Path) -> Path:
    """Normalize raw Databento CME GC trades, ensuring strictly increasing UTC ns timestamps."""
    out, _ = load_and_clean_trades(raw_input_path=raw_input_path, output_path=output_path)
    return out


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
