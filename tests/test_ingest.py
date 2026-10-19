"""Unit tests for src/ingest.py.

All tests mock the Databento API client to ensure zero live network calls or spend.
Tests verify:
- Idempotency (skipping existing partitions and empty markers)
- Empty-day marker generation (_empty)
- Retry with exponential backoff on transient errors
- Cost guard behavior (dry-run, missing approval, cost exceeded, approved run)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src import ingest
from src.ingest import (
    CostLimitExceededError,
    IngestionError,
    pull_day,
    pull_range,
)


def _create_mock_store(df: pd.DataFrame) -> MagicMock:
    """Helper to create a mock DBNStore returning a DataFrame."""
    mock_store = MagicMock()
    mock_store.to_df.return_value = df
    return mock_store


def test_pull_day_success(tmp_path: Path) -> None:
    """Verify pull_day creates zstd-compressed parquet with filtered columns."""
    sample_df = pd.DataFrame(
        {
            "ts_event": [1704153600000000000, 1704153601000000000],
            "ts_recv": [1704153600000100000, 1704153601000100000],
            "instrument_id": [1234, 1234],
            "symbol": ["GC.v.0", "GC.v.0"],
            "side": ["A", "B"],
            "price": [2050.10, 2050.20],
            "size": [1, 2],
            "flags": [0, 0],
            "sequence": [101, 102],
            "action": ["T", "T"],
            "extra_unneeded_col": ["foo", "bar"],
        }
    )

    mock_client = MagicMock()
    mock_client.timeseries.get_range.return_value = _create_mock_store(sample_df)

    target_date = date(2024, 1, 2)
    parquet_path = pull_day(
        target_date=target_date,
        client=mock_client,
        output_base_dir=tmp_path,
    )

    assert parquet_path.exists()
    assert parquet_path.name == "part.parquet"
    assert parquet_path.parent.name == "date=2024-01-02"

    # Read back parquet and verify schema & data
    table = pq.read_table(parquet_path)
    assert table.num_rows == 2
    assert "extra_unneeded_col" not in table.column_names
    assert "ts_event" in table.column_names
    assert "price" in table.column_names


def test_pull_day_idempotency_existing_parquet(tmp_path: Path) -> None:
    """Verify pull_day skips API call when partition parquet already exists."""
    date_str = "2024-01-03"
    part_dir = tmp_path / f"date={date_str}"
    part_dir.mkdir(parents=True)
    existing_file = part_dir / "part.parquet"
    existing_file.write_text("existing dummy content")

    mock_client = MagicMock()
    # If client is touched, it would raise
    mock_client.timeseries.get_range.side_effect = AssertionError("Should not call client")

    result_path = pull_day(
        target_date=date_str,
        client=mock_client,
        output_base_dir=tmp_path,
    )

    assert result_path == existing_file
    mock_client.timeseries.get_range.assert_not_called()


def test_pull_day_empty_day_marker(tmp_path: Path) -> None:
    """Verify empty DataFrame writes _empty marker and second call is idempotent."""
    mock_client = MagicMock()
    empty_df = pd.DataFrame()
    mock_client.timeseries.get_range.return_value = _create_mock_store(empty_df)

    target_date = "2024-01-04"
    result_path = pull_day(
        target_date=target_date,
        client=mock_client,
        output_base_dir=tmp_path,
    )

    assert result_path.exists()
    assert result_path.name == "_empty"
    assert not (tmp_path / f"date={target_date}" / "part.parquet").exists()

    # Second call should skip immediately without calling API
    mock_client.timeseries.get_range.side_effect = AssertionError("Should not be called")
    second_path = pull_day(
        target_date=target_date,
        client=mock_client,
        output_base_dir=tmp_path,
    )
    assert second_path == result_path


def test_pull_day_retries_transient_failure(tmp_path: Path) -> None:
    """Verify pull_day retries on transient errors and succeeds."""
    sample_df = pd.DataFrame({"ts_event": [1], "price": [2000.0]})

    mock_client = MagicMock()
    # Fail twice with ConnectionError, succeed on 3rd attempt
    mock_client.timeseries.get_range.side_effect = [
        ConnectionError("Network glitch 1"),
        ConnectionError("Network glitch 2"),
        _create_mock_store(sample_df),
    ]

    result_path = pull_day(
        target_date="2024-01-05",
        client=mock_client,
        output_base_dir=tmp_path,
        max_retries=3,
        backoff_base_sec=0.01,  # Fast backoff for tests
    )

    assert result_path.exists()
    assert mock_client.timeseries.get_range.call_count == 3


def test_pull_day_retries_exhaustion(tmp_path: Path) -> None:
    """Verify IngestionError is raised when max retries are exhausted."""
    mock_client = MagicMock()
    mock_client.timeseries.get_range.side_effect = ConnectionError("Persistent timeout")

    with pytest.raises(IngestionError, match="Failed to ingest trades"):
        pull_day(
            target_date="2024-01-06",
            client=mock_client,
            output_base_dir=tmp_path,
            max_retries=2,
            backoff_base_sec=0.01,
        )

    assert mock_client.timeseries.get_range.call_count == 3  # initial + 2 retries


def test_cost_guard_dry_run(tmp_path: Path) -> None:
    """Verify dry-run checks cost and exits without pulling data."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 4.25

    results = pull_range(
        start="2024-01-02",
        end="2024-01-03",
        dry_run=True,
        client=mock_client,
        output_base_dir=tmp_path,
    )

    assert results == []
    mock_client.metadata.get_cost.assert_called_once()
    mock_client.timeseries.get_range.assert_not_called()


def test_cost_guard_abort_without_approval(tmp_path: Path) -> None:
    """Verify real run aborts if --approve-usd is omitted."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 2.50

    with pytest.raises(CostLimitExceededError, match="no approval limit was provided"):
        pull_range(
            start="2024-01-02",
            end="2024-01-02",
            approve_usd=None,
            dry_run=False,
            client=mock_client,
            output_base_dir=tmp_path,
        )

    mock_client.timeseries.get_range.assert_not_called()


def test_cost_guard_abort_cost_exceeds_limit(tmp_path: Path) -> None:
    """Verify real run aborts if estimated cost exceeds approved USD limit."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 15.00

    with pytest.raises(CostLimitExceededError, match="exceeds the approved maximum"):
        pull_range(
            start="2024-01-02",
            end="2024-01-02",
            approve_usd=10.00,
            dry_run=False,
            client=mock_client,
            output_base_dir=tmp_path,
        )

    mock_client.timeseries.get_range.assert_not_called()


def test_cost_guard_proceeds_when_within_approval(tmp_path: Path) -> None:
    """Verify pull_range executes pull_day when estimated cost is within approved limit."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 3.50
    sample_df = pd.DataFrame({"ts_event": [100], "price": [2060.0]})
    mock_client.timeseries.get_range.return_value = _create_mock_store(sample_df)

    results = pull_range(
        start="2024-01-02",
        end="2024-01-03",
        approve_usd=5.00,
        dry_run=False,
        client=mock_client,
        output_base_dir=tmp_path,
    )

    assert len(results) == 2
    assert all(p.exists() for p in results)
    assert mock_client.timeseries.get_range.call_count == 2


def test_cli_dry_run_invocation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI dry run executes get_cost and exits cleanly with 0."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 1.25

    test_args = ["ingest", "--start", "2024-01-02", "--end", "2024-01-02", "--dry-run"]
    monkeypatch.setattr("sys.argv", test_args)

    with patch("src.ingest.get_historical_client", return_value=mock_client):
        ingest.main()

    captured = capsys.readouterr()
    assert "[COST CHECK]" in captured.out
    assert "$1.2500 USD" in captured.out
    assert "[DRY RUN]" in captured.out
    mock_client.timeseries.get_range.assert_not_called()


def test_cli_cost_limit_exceeded_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CLI exits with code 2 when cost exceeds approved limit."""
    mock_client = MagicMock()
    mock_client.metadata.get_cost.return_value = 10.00

    test_args = [
        "ingest",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-02",
        "--approve-usd",
        "5.00",
    ]
    monkeypatch.setattr("sys.argv", test_args)

    with patch("src.ingest.get_historical_client", return_value=mock_client):
        with pytest.raises(SystemExit) as exc_info:
            ingest.main()
        assert exc_info.value.code == 2

