"""Data ingestion module for CME GC trades (Databento) and broker XAUUSD ticks (MT5).

Hard Rules:
- Raw data in data/raw* is IMMUTABLE. Never edit, overwrite or delete it.
- NETWORK/SPEND: before ANY Databento pull, print metadata.get_cost() and STOP for approval.
- Databento `side` semantics, contract-roll handling and MT5 server-time offset are uncertain:
  STOP and ask if docs/data don't settle it.
"""

from __future__ import annotations

from pathlib import Path

from src.config import RAW_DATA_DIR


def check_databento_cost(dataset: str, symbols: list[str], start: str, end: str) -> float:
    """Print metadata.get_cost() and return estimated cost before any Databento download.

    STOP for user approval before making network pulls.
    """
    raise NotImplementedError("Databento cost check stub - requires user approval before execution")


def ingest_databento_gc(
    start_date: str,
    end_date: str,
    output_dir: Path = RAW_DATA_DIR,
) -> Path:
    """Ingest CME GC trade data from Databento into immutable raw storage.

    All raw output files are read-only and immutable.
    """
    raise NotImplementedError("Databento GC ingestion stub")


def ingest_mt5_ticks(
    file_path: Path,
    output_dir: Path = RAW_DATA_DIR,
) -> Path:
    """Ingest raw MT5 broker XAUUSD tick export into raw storage."""
    raise NotImplementedError("MT5 ticks ingestion stub")
