"""Append-only trial logger for statistical tests.

Per AGENTS.md:
Every statistical test calls trials.log(name) (src/trials.py).
The trial count feeds the Deflated Sharpe calculation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.config import TRIALS_PATH


def log(name: str, path: Path | None = None, **metadata: Any) -> int:
    """Log an event study or statistical test execution.

    Args:
        name: Identifier or description of the statistical test / hypothesis run.
        path: Path to the trials JSONL file (defaults to data/trials.jsonl).
        **metadata: Additional attributes to record (e.g., test_type, p_value, t_stat).

    Returns:
        The updated total count of registered trials.
    """
    target_path = path or TRIALS_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "name": name,
        "timestamp_utc_ns": time.time_ns(),
        "metadata": metadata,
    }

    with open(target_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")

    return count(target_path)


def count(path: Path | None = None) -> int:
    """Count the total number of logged trials in the trials log.

    Args:
        path: Path to the trials JSONL file (defaults to data/trials.jsonl).

    Returns:
        Integer count of trials logged.
    """
    target_path = path or TRIALS_PATH
    if not target_path.exists():
        return 0

    total = 0
    with open(target_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                total += 1
    return total


def read_trials(path: Path | None = None) -> list[dict[str, Any]]:
    """Read all logged trials into a list of dictionaries.

    Args:
        path: Path to the trials JSONL file (defaults to data/trials.jsonl).

    Returns:
        List of trial records.
    """
    target_path = path or TRIALS_PATH
    if not target_path.exists():
        return []

    trials: list[dict[str, Any]] = []
    with open(target_path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                trials.append(json.loads(line_str))
    return trials
