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

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.config import (
    PROCESSED_DATA_DIR,
    TICK_SIZE_GC,
    VALUE_AREA_PCT,
)

SESSION_COLUMNS: list[str] = [
    "trade_date",
    "sub",
    "instrument_id",
    "o",
    "h",
    "l",
    "c",
    "vol",
    "buy_v",
    "sell_v",
    "n_trades",
    "poc",
    "val",
    "vah",
    "t_start",
    "t_end",
]

SUB_SESSION_ORDER: dict[str, int] = {
    "asia": 1,
    "london": 2,
    "ny": 3,
}


def volume_profile(
    price: np.ndarray | pd.Series | list[float],
    size: np.ndarray | pd.Series | list[float],
    tick: float = TICK_SIZE_GC,
    va: float = VALUE_AREA_PCT,
) -> tuple[float, float, float, np.ndarray]:
    """Calculate Point of Control (POC), Value Area Low (VAL), VAH, and profile array.

    Vectorized bincount with value area expanded from POC two bins at a time
    toward the heavier side.

    Args:
        price: Array-like of trade prices.
        size: Array-like of trade volumes/sizes.
        tick: Discretization tick size (default GC = 0.10).
        va: Target volume fraction for value area (default 0.70).

    Returns:
        Tuple of (poc, val, vah, profile_array).
    """
    price_arr = np.asarray(price, dtype=np.float64)
    size_arr = np.asarray(size, dtype=np.float64)

    if len(price_arr) == 0:
        raise ValueError("Cannot compute volume profile on empty data")

    total_volume = float(np.sum(size_arr))
    if total_volume <= 0:
        p = round(float(price_arr[0]), 4)
        return p, p, p, np.array([0.0], dtype=np.float64)

    min_p = float(np.min(price_arr))
    max_p = float(np.max(price_arr))
    num_bins = int(np.round((max_p - min_p) / tick)) + 1

    # Map prices to bin indices and clip to valid range
    bin_indices = np.clip(
        np.round((price_arr - min_p) / tick).astype(np.int64),
        0,
        num_bins - 1,
    )
    profile = np.bincount(bin_indices, weights=size_arr, minlength=num_bins)

    # Point of Control (highest volume bin)
    poc_idx = int(np.argmax(profile))
    target_volume = va * total_volume

    current_vol = float(profile[poc_idx])
    lower_idx = poc_idx
    upper_idx = poc_idx

    # Value area expanded two bins at a time toward the heavier side
    while current_vol < target_volume and (lower_idx > 0 or upper_idx < num_bins - 1):
        up_v = 0.0
        up_step = 0
        if upper_idx + 1 < num_bins:
            up_v += profile[upper_idx + 1]
            up_step = 1
            if upper_idx + 2 < num_bins:
                up_v += profile[upper_idx + 2]
                up_step = 2

        down_v = 0.0
        down_step = 0
        if lower_idx - 1 >= 0:
            down_v += profile[lower_idx - 1]
            down_step = 1
            if lower_idx - 2 >= 0:
                down_v += profile[lower_idx - 2]
                down_step = 2

        if up_step == 0 and down_step == 0:
            break

        if up_step > 0 and down_step > 0:
            if up_v > down_v:
                upper_idx += up_step
                current_vol += up_v
            elif down_v > up_v:
                lower_idx -= down_step
                current_vol += down_v
            else:
                # Tie: expand both sides
                upper_idx += up_step
                lower_idx -= down_step
                current_vol += up_v + down_v
        elif up_step > 0:
            upper_idx += up_step
            current_vol += up_v
        else:
            lower_idx -= down_step
            current_vol += down_v

    decimals = max(2, int(np.ceil(-np.log10(tick))) + 2) if tick < 1.0 else 2
    val = round(min_p + lower_idx * tick, decimals)
    vah = round(min_p + upper_idx * tick, decimals)
    poc = round(min_p + poc_idx * tick, decimals)

    return poc, val, vah, profile


def compute_volume_profile(
    prices: Any,
    volumes: Any,
    tick_size: float = TICK_SIZE_GC,
    value_area_pct: float = VALUE_AREA_PCT,
) -> dict[str, Any]:
    """Calculate Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL).

    Args:
        prices: Array-like of trade/bar prices.
        volumes: Array-like of trade/bar volumes.
        tick_size: Price discretization unit (GC = 0.10).
        value_area_pct: Target volume fraction for value area (default 0.70).

    Returns:
        Dict with keys: 'poc', 'vah', 'val', 'profile'.
    """
    poc, val, vah, profile = volume_profile(
        prices, volumes, tick=tick_size, va=value_area_pct
    )
    return {
        "poc": poc,
        "val": val,
        "vah": vah,
        "profile": profile,
    }


def build_sessions_table(
    clean_trades_input: Path | pd.DataFrame | str,
    output_path: Path = PROCESSED_DATA_DIR / "sessions",
    tick_size: float = TICK_SIZE_GC,
    value_area_pct: float = VALUE_AREA_PCT,
) -> Path:
    """Build the sessions Parquet table partitioned by trade_date.

    Args:
        clean_trades_input: Cleaned trades DataFrame or Path to Parquet file(s).
        output_path: Root destination directory for partitioned sessions table.
        tick_size: Price discretization unit (default GC = 0.10).
        value_area_pct: Target volume fraction for value area (default 0.70).

    Returns:
        Path to output directory.
    """
    if isinstance(clean_trades_input, pd.DataFrame):
        trades_df = clean_trades_input.copy()
    else:
        path = Path(clean_trades_input)
        if path.is_file():
            trades_df = pd.read_parquet(path)
        else:
            files = list(path.glob("**/*.parquet"))
            if not files:
                out_dir = Path(output_path)
                out_dir.mkdir(parents=True, exist_ok=True)
                return out_dir
            # Use DuckDB to load all parquet files efficiently
            con = duckdb.connect()
            trades_df = con.execute(
                f"SELECT * FROM read_parquet('{path}/**/*.parquet')"
            ).df()

    if trades_df.empty:
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    # Exclude maintenance break or unassigned trades (sub is null)
    valid_sessions = trades_df[trades_df["sub"].notna()].copy()
    if valid_sessions.empty:
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    # Sort trades chronologically within each group
    sort_cols = ["ts_event"]
    if "sequence" in valid_sessions.columns:
        sort_cols.append("sequence")
    valid_sessions = valid_sessions.sort_values(by=sort_cols, ascending=True)

    session_records = []
    # Group by (trade_date, sub, instrument_id)
    grouped = valid_sessions.groupby(
        ["trade_date", "sub", "instrument_id"],
        as_index=False,
        sort=False,
    )

    for (trade_date, sub, inst_id), group in grouped:
        prices = group["price"].to_numpy()
        sizes = group["size"].to_numpy()
        sgns = group["sgn"].to_numpy()
        ts_events = group["ts_event"].to_numpy()

        poc, val, vah, _ = volume_profile(
            prices, sizes, tick=tick_size, va=value_area_pct
        )

        vol = int(np.sum(sizes))
        buy_v = int(np.sum(sizes[sgns > 0]))
        sell_v = int(np.sum(sizes[sgns < 0]))
        n_trades = int(len(group))

        session_records.append({
            "trade_date": str(trade_date),
            "sub": str(sub),
            "instrument_id": int(inst_id),
            "o": float(prices[0]),
            "h": float(np.max(prices)),
            "l": float(np.min(prices)),
            "c": float(prices[-1]),
            "vol": vol,
            "buy_v": buy_v,
            "sell_v": sell_v,
            "n_trades": n_trades,
            "poc": float(poc),
            "val": float(val),
            "vah": float(vah),
            "t_start": int(ts_events[0]),
            "t_end": int(ts_events[-1]),
        })

    sessions_df = pd.DataFrame(session_records, columns=SESSION_COLUMNS)

    # Sort sessions by trade_date, sub_session order, instrument_id
    sessions_df["sub_order"] = sessions_df["sub"].map(SUB_SESSION_ORDER).fillna(99)
    sessions_df = sessions_df.sort_values(
        by=["trade_date", "sub_order", "instrument_id"],
        ascending=True,
    ).drop(columns=["sub_order"]).reset_index(drop=True)

    # Output Hive-partitioned Parquet (trade_date=YYYY-MM-DD/part.parquet)
    out_dir = Path(output_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    for date_str, group in sessions_df.groupby("trade_date"):
        partition_dir = out_dir / f"trade_date={date_str}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(group)
        pq.write_table(table, partition_dir / "part.parquet", compression="zstd")

    return out_dir
