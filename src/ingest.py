"""Data ingestion module for CME GC trades from Databento.

Governed by AGENTS.md, docs/SPEC.md, and docs/acceptance.yaml.
- Raw data in data/raw* is IMMUTABLE.
- All network pulls must be cost-checked beforehand.
- Idempotent: existing partitions and empty-day markers are skipped.
- Automatic retries with exponential backoff on transient errors.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.config import RAW_DATA_DIR

# --- Databento Acquisition Constants ---
DATASET: str = "GLBX.MDP3"
SCHEMA: str = "trades"
SYMBOL: str = "GC.v.0"
STYPE_IN: str = "continuous"

KEPT_COLUMNS: list[str] = [
    "ts_event",
    "ts_recv",
    "instrument_id",
    "symbol",
    "side",
    "price",
    "size",
    "flags",
    "sequence",
    "action",
]

DEFAULT_RAW_GC_DIR: Path = RAW_DATA_DIR / "gc_trades"


class CostLimitExceededError(ValueError):
    """Raised when estimated Databento cost exceeds the approved USD threshold."""


class IngestionError(RuntimeError):
    """Raised when data ingestion fails after exhausting retries."""


def get_historical_client(api_key: str | None = None) -> Any:
    """Create a Databento Historical client.

    Retrieves key from DATABENTO_API_KEY environment variable if not passed.
    Never prints or logs the API key.
    """
    import databento

    key = api_key or os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise ValueError("DATABENTO_API_KEY environment variable is not set.")
    return databento.Historical(key=key)


def get_cost_for_range(
    client: Any,
    start: str | date,
    end: str | date,
    dataset: str = DATASET,
    symbol: str = SYMBOL,
    schema: str = SCHEMA,
    stype_in: str = STYPE_IN,
) -> float:
    """Query Databento metadata.get_cost() for a specified date range.

    Args:
        client: Databento Historical client (or mock).
        start: Start date (inclusive).
        end: End date (exclusive for API range).
        dataset: Databento dataset ID.
        symbol: Parent/continuous symbology.
        schema: Data schema ('trades').
        stype_in: Symbology type ('continuous').

    Returns:
        Estimated query cost in USD.
    """
    start_str = start.isoformat() if isinstance(start, date) else str(start)
    end_str = end.isoformat() if isinstance(end, date) else str(end)

    cost = client.metadata.get_cost(
        dataset=dataset,
        symbols=symbol,
        schema=schema,
        stype_in=stype_in,
        start=start_str,
        end=end_str,
    )
    return float(cost)


def pull_day(
    target_date: str | date,
    client: Any = None,
    output_base_dir: Path = DEFAULT_RAW_GC_DIR,
    max_retries: int = 4,
    backoff_base_sec: float = 1.0,
) -> Path:
    """Ingest one day of CME GC trades from Databento into immutable raw storage.

    Path format: data/raw/gc_trades/date=YYYY-MM-DD/part.parquet (zstd compressed).
    If no trades occurred (empty day), writes an empty marker: date=YYYY-MM-DD/_empty.

    Idempotent:
        Skips execution if either part.parquet or _empty exists.

    Retries:
        Exponential backoff on network/transient failures.
    """
    d_obj = (
        target_date
        if isinstance(target_date, date)
        else datetime.strptime(str(target_date)[:10], "%Y-%m-%d").date()
    )
    date_str = d_obj.isoformat()
    partition_dir = output_base_dir / f"date={date_str}"
    parquet_path = partition_dir / "part.parquet"
    empty_marker_path = partition_dir / "_empty"

    # Idempotency check: skip existing files
    if parquet_path.exists():
        return parquet_path
    if empty_marker_path.exists():
        return empty_marker_path

    if client is None:
        client = get_historical_client()

    # Query 24-hour UTC window for this calendar date
    start_ts = f"{date_str}T00:00:00"
    end_ts = (d_obj + timedelta(days=1)).isoformat() + "T00:00:00"

    attempt = 0
    last_err: Exception | None = None

    while attempt <= max_retries:
        try:
            store = client.timeseries.get_range(
                dataset=DATASET,
                symbols=SYMBOL,
                schema=SCHEMA,
                stype_in=STYPE_IN,
                start=start_ts,
                end=end_ts,
            )
            df = store.to_df()
            break
        except Exception as exc:
            last_err = exc
            attempt += 1
            if attempt > max_retries:
                break
            sleep_sec = backoff_base_sec * (2 ** (attempt - 1))
            time.sleep(sleep_sec)

    if attempt > max_retries:
        raise IngestionError(
            f"Failed to ingest trades for {date_str} after {max_retries} retries: {last_err}"
        ) from last_err

    partition_dir.mkdir(parents=True, exist_ok=True)

    # Empty day handling
    if df is None or len(df) == 0:
        empty_marker_path.touch(exist_ok=True)
        return empty_marker_path

    # Standardize index and extract required columns
    if "ts_event" not in df.columns and df.index.name in ("ts_event", "index"):
        df = df.reset_index()

    available_cols = [c for c in KEPT_COLUMNS if c in df.columns]
    df_filtered = df[available_cols].copy()

    # Write as zstd-compressed Parquet table
    table = pa.Table.from_pandas(df_filtered, preserve_index=False)
    pq.write_table(table, parquet_path, compression="zstd")

    return parquet_path


def pull_range(
    start: str | date,
    end: str | date,
    approve_usd: float | None = None,
    dry_run: bool = False,
    client: Any = None,
    output_base_dir: Path = DEFAULT_RAW_GC_DIR,
) -> list[Path]:
    """Execute dry-run cost check or ingest a contiguous range of dates with cost guard.

    Args:
        start: Start date (inclusive).
        end: End date (inclusive).
        approve_usd: Maximum allowed spend in USD.
        dry_run: If True, prints estimated cost and returns empty list without pulling.
        client: Databento client (or mock).
        output_base_dir: Target directory for raw files.

    Returns:
        List of generated/verified partition paths.
    """
    d_start = (
        start if isinstance(start, date) else datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
    )
    d_end = end if isinstance(end, date) else datetime.strptime(str(end)[:10], "%Y-%m-%d").date()

    if d_start > d_end:
        raise ValueError(f"Start date {d_start} cannot be after end date {d_end}")

    if client is None:
        client = get_historical_client()

    api_end_date = d_end + timedelta(days=1)
    estimated_cost = get_cost_for_range(client, start=d_start, end=api_end_date)
    print(f"[COST CHECK] Range {d_start} to {d_end}: Estimated cost = ${estimated_cost:.4f} USD")

    if dry_run:
        print("[DRY RUN] Exiting without pulling data.")
        return []

    if approve_usd is None:
        raise CostLimitExceededError(
            f"Execution aborted: Estimated cost is ${estimated_cost:.4f} USD, "
            "but no approval limit was provided via --approve-usd."
        )

    if estimated_cost > approve_usd:
        raise CostLimitExceededError(
            f"Execution aborted: Estimated cost ${estimated_cost:.4f} USD exceeds "
            f"the approved maximum of ${approve_usd:.4f} USD."
        )

    print(
        f"[APPROVED] Estimated cost ${estimated_cost:.4f} USD <= "
        f"approved limit ${approve_usd:.4f} USD. Proceeding with ingestion..."
    )

    results: list[Path] = []
    curr = d_start
    while curr <= d_end:
        path = pull_day(curr, client=client, output_base_dir=output_base_dir)
        results.append(path)
        curr += timedelta(days=1)

    return results


def main() -> None:
    """CLI entry point for Databento ingestion."""
    parser = argparse.ArgumentParser(
        description="Ingest CME GC trade data from Databento with cost guard and idempotency."
    )
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print estimated cost from metadata.get_cost() and exit.",
    )
    parser.add_argument(
        "--approve-usd",
        type=float,
        default=None,
        help="Maximum spend allowed in USD. Required for a live run.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_RAW_GC_DIR,
        help="Base output directory for raw data.",
    )

    args = parser.parse_args()

    try:
        paths = pull_range(
            start=args.start,
            end=args.end,
            approve_usd=args.approve_usd,
            dry_run=args.dry_run,
            output_base_dir=args.output_dir,
        )
        if not args.dry_run:
            print(f"Successfully processed {len(paths)} partition(s).")
    except CostLimitExceededError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(2)
    except Exception as err:
        print(f"Ingestion failed: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
