"""Derived bar construction module for 1-second and volume bars.

Governed by docs/SPEC.md:
- bars_1s: ts, instrument_id, o, h, l, c, v, buy_v, sell_v, n
- volume_bars: trade_date, bar, o, h, l, c, v, delta, t0, t1
- Volume bars: cumulative size // bar, reset per trade_date.
- Bar size auto-calibrated per month to hit 100-200 bars/day.
- Partitioned Parquet output (zstd).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.config import (
    PROCESSED_DATA_DIR,
    TARGET_VOLUME_BARS_PER_DAY_MAX,
    TARGET_VOLUME_BARS_PER_DAY_MIN,
)


def calibrate_volume_bar_size(
    df_or_summary: pd.DataFrame,
    target_bars_per_day: int = 150,
) -> int:
    """Calibrate volume bar threshold to hit target bars per day (100-200 bars/day).

    Args:
        df_or_summary: DataFrame containing 'size' and 'trade_date'.
        target_bars_per_day: Desired daily bar count (default 150, target range 100-200).

    Returns:
        Integer volume threshold per bar.
    """
    if df_or_summary.empty:
        return 100

    total_volume = int(df_or_summary["size"].sum())
    n_days = max(1, df_or_summary["trade_date"].nunique())
    avg_daily_volume = total_volume / n_days

    # Clamp target bars per day to spec bounds [100, 200]
    clamped_target = max(
        TARGET_VOLUME_BARS_PER_DAY_MIN,
        min(TARGET_VOLUME_BARS_PER_DAY_MAX, target_bars_per_day),
    )

    bar_size = max(1, int(round(avg_daily_volume / clamped_target)))
    return bar_size


def derive_bars_1s_from_df(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trade data into 1-second OHLCV bars via DuckDB SQL.

    Schema: ts, instrument_id, o, h, l, c, v, buy_v, sell_v, unsided_v, n
    """
    if df.empty:
        return pd.DataFrame(
            columns=[
                "ts",
                "instrument_id",
                "trade_date",
                "o",
                "h",
                "l",
                "c",
                "v",
                "buy_v",
                "sell_v",
                "unsided_v",
                "n",
            ]
        )

    con = duckdb.connect(database=":memory:")
    query = """
        SELECT
            (ts_event // 1000000000) * 1000000000 AS ts,
            instrument_id,
            trade_date,
            FIRST(price ORDER BY ts_event ASC, sequence ASC) AS o,
            MAX(price) AS h,
            MIN(price) AS l,
            LAST(price ORDER BY ts_event ASC, sequence ASC) AS c,
            SUM(size)::BIGINT AS v,
            SUM(CASE WHEN sgn = 1 THEN size ELSE 0 END)::BIGINT AS buy_v,
            SUM(CASE WHEN sgn = -1 THEN size ELSE 0 END)::BIGINT AS sell_v,
            SUM(CASE WHEN sgn = 0 THEN size ELSE 0 END)::BIGINT AS unsided_v,
            COUNT(*)::BIGINT AS n
        FROM df
        GROUP BY (ts_event // 1000000000) * 1000000000, instrument_id, trade_date
        ORDER BY ts ASC
    """
    bars_df = con.execute(query).df()
    con.close()
    return bars_df


def derive_volume_bars_from_df(
    df: pd.DataFrame,
    bar_size: int | None = None,
    target_bars_per_day: int = 150,
) -> pd.DataFrame:
    """Construct dynamic volume bars with cumulative size // bar, reset per trade_date.

    Schema: trade_date, bar, o, h, l, c, v, delta, t0, t1
    """
    if df.empty:
        return pd.DataFrame(
            columns=["trade_date", "bar", "o", "h", "l", "c", "v", "delta", "t0", "t1"]
        )

    effective_bar_size = (
        bar_size
        if bar_size is not None
        else calibrate_volume_bar_size(df, target_bars_per_day=target_bars_per_day)
    )

    con = duckdb.connect(database=":memory:")
    query = f"""
        WITH cumulative AS (
            SELECT
                *,
                (SUM(size) OVER (PARTITION BY trade_date ORDER BY ts_event ASC, sequence ASC) - 1)
                // {effective_bar_size} AS bar
            FROM df
        )
        SELECT
            trade_date,
            bar::BIGINT AS bar,
            FIRST(price ORDER BY ts_event ASC, sequence ASC) AS o,
            MAX(price) AS h,
            MIN(price) AS l,
            LAST(price ORDER BY ts_event ASC, sequence ASC) AS c,
            SUM(size)::BIGINT AS v,
            SUM(sv)::BIGINT AS delta,
            MIN(ts_event)::BIGINT AS t0,
            MAX(ts_event)::BIGINT AS t1
        FROM cumulative
        GROUP BY trade_date, bar
        ORDER BY trade_date ASC, bar ASC
    """
    vol_bars_df = con.execute(query).df()
    con.close()
    return vol_bars_df


def build_bars_1s(
    clean_trades_path: Path,
    output_dir: Path = PROCESSED_DATA_DIR / "bars_1s",
) -> Path:
    """Build 1-second OHLCV bars table partitioned by trade_date in Parquet."""
    con = duckdb.connect(database=":memory:")
    path_str = str(clean_trades_path).replace("\\", "/")
    df = con.execute(f"SELECT * FROM read_parquet('{path_str}')").df()
    con.close()

    bars_df = derive_bars_1s_from_df(df)
    output_dir.mkdir(parents=True, exist_ok=True)

    for trade_date, group in bars_df.groupby("trade_date"):
        partition_dir = output_dir / f"trade_date={trade_date}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        out_file = partition_dir / "part.parquet"
        tbl = pa.Table.from_pandas(group, preserve_index=False)
        pq.write_table(tbl, out_file, compression="zstd")

    return output_dir


def build_volume_bars(
    clean_trades_path: Path,
    output_dir: Path = PROCESSED_DATA_DIR / "volume_bars",
    bar_size: int | None = None,
    target_bars_per_day: int = 150,
) -> Path:
    """Build volume bars table partitioned by trade_date in Parquet."""
    con = duckdb.connect(database=":memory:")
    path_str = str(clean_trades_path).replace("\\", "/")
    df = con.execute(f"SELECT * FROM read_parquet('{path_str}')").df()
    con.close()

    vol_bars_df = derive_volume_bars_from_df(
        df, bar_size=bar_size, target_bars_per_day=target_bars_per_day
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    for trade_date, group in vol_bars_df.groupby("trade_date"):
        partition_dir = output_dir / f"trade_date={trade_date}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        out_file = partition_dir / "part.parquet"
        tbl = pa.Table.from_pandas(group, preserve_index=False)
        pq.write_table(tbl, out_file, compression="zstd")

    return output_dir
