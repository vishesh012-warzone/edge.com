"""Event study framework and pre-registered hypothesis testing.

Evaluates Parquet table:
- events: trade_date, state, outcomes (see docs/prereg.md)

Research Integrity:
- Every statistical test must call trials.log(name) (src/trials.py).
- No tuning on out-of-sample data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src import trials
from src.config import PROCESSED_DATA_DIR


def detect_events(
    sessions_path: Path,
    volume_bars_path: Path,
    output_path: Path = PROCESSED_DATA_DIR / "events",
) -> Path:
    """Identify pre-registered event states (e.g., OPEN_OUT_VA_REENTER, ABSORPTION_HIGH)."""
    raise NotImplementedError("Detect events stub")


def run_event_study(
    events_path: Path,
    hypothesis_name: str,
) -> dict[str, Any]:
    """Execute pre-registered hypothesis test and log trial to data/trials.jsonl."""
    # Every test must record trial count
    trial_num = trials.log(hypothesis_name)
    raise NotImplementedError(f"Event study '{hypothesis_name}' stub (trial #{trial_num})")
