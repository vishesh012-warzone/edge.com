"""Tests for trials append-only logging module."""

from pathlib import Path

from src import trials


def test_trials_logging(tmp_path: Path) -> None:
    """Verify trials log appends records and correctly maintains count."""
    test_log_file = tmp_path / "test_trials.jsonl"

    assert trials.count(test_log_file) == 0

    c1 = trials.log("H1_value_area_reentry", path=test_log_file, p_val=0.005)
    assert c1 == 1
    assert trials.count(test_log_file) == 1

    c2 = trials.log("H2_delta_absorption", path=test_log_file, t_stat=3.12)
    assert c2 == 2
    assert trials.count(test_log_file) == 2

    records = trials.read_trials(test_log_file)
    assert len(records) == 2
    assert records[0]["name"] == "H1_value_area_reentry"
    assert records[0]["metadata"]["p_val"] == 0.005
    assert records[1]["name"] == "H2_delta_absorption"
    assert records[1]["metadata"]["t_stat"] == 3.12
