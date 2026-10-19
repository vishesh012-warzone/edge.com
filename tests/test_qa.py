"""Unit and regression tests for src/qa.py QA reporting and data integrity checks."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.clean import clean_trades_dataframe
from src.profile import build_sessions_table
from src.qa import (
    check_contract_rolls,
    check_daily_volume_outliers,
    check_profile_invariants,
    check_session_counts,
    check_zero_volume_days,
    run_qa,
)

TZ_ET = ZoneInfo("America/New_York")


@pytest.fixture
def valid_sessions_and_trades(tmp_path):
    """Fixture providing valid sample sessions and clean trades."""
    t_asia = datetime(2026, 10, 18, 19, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    t_london = datetime(2026, 10, 19, 4, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    t_ny = datetime(2026, 10, 19, 10, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)

    ts_asia = int(t_asia.timestamp() * 1e9)
    ts_london = int(t_london.timestamp() * 1e9)
    ts_ny = int(t_ny.timestamp() * 1e9)

    raw_df = pd.DataFrame(
        {
            "ts_event": [
                ts_asia,
                ts_asia + 1_000_000,
                ts_london,
                ts_london + 1_000_000,
                ts_ny,
                ts_ny + 1_000_000,
            ],
            "sequence": [1, 2, 1, 2, 1, 2],
            "action": ["T", "T", "T", "T", "T", "T"],
            "size": [10, 20, 15, 25, 30, 40],
            "price": [2650.1, 2650.3, 2652.0, 2652.5, 2655.0, 2654.8],
            "side": ["A", "B", "A", "B", "A", "B"],
            "instrument_id": [1001, 1001, 1001, 1001, 1001, 1001],
        }
    )

    clean_df, _ = clean_trades_dataframe(raw_df)
    sessions_dir = tmp_path / "sessions"
    build_sessions_table(clean_df, output_path=sessions_dir)

    parquet_files = list(sessions_dir.glob("trade_date=*/part.parquet"))
    sessions_df = pd.concat([pd.read_parquet(p) for p in parquet_files], ignore_index=True)

    return sessions_df, clean_df, sessions_dir


def test_qa_passes_on_valid_dataset(valid_sessions_and_trades, tmp_path):
    """Test run_qa passes completely on valid dataset."""
    sessions_df, clean_df, sessions_dir = valid_sessions_and_trades
    json_path = tmp_path / "qa_report.json"
    md_path = tmp_path / "qa_report.md"

    report = run_qa(
        sessions_input=sessions_df,
        clean_trades_input=clean_df,
        output_json=json_path,
        output_md=md_path,
        raise_on_failure=False,
    )

    assert report["status"] == "PASSED"
    assert json_path.exists()
    assert md_path.exists()

    # Verify JSON content
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] == "PASSED"
    assert "checks" in data
    assert data["checks"]["invariants"]["passed"] is True
    assert data["checks"]["zero_volume"]["passed"] is True
    assert data["checks"]["contract_rolls"]["passed"] is True


def test_qa_fails_on_invariant_violation(valid_sessions_and_trades, tmp_path):
    """Test QA fails when an invariant is violated (VAL > VAH)."""
    sessions_df, clean_df, _ = valid_sessions_and_trades
    corrupted_sessions = sessions_df.copy()
    # Invert VAL and VAH to violate invariant
    corrupted_sessions.loc[0, "val"] = 2700.0
    corrupted_sessions.loc[0, "vah"] = 2600.0

    inv_res = check_profile_invariants(corrupted_sessions, clean_df)
    assert inv_res["passed"] is False

    json_path = tmp_path / "fail_report.json"
    report = run_qa(
        sessions_input=corrupted_sessions,
        clean_trades_input=clean_df,
        output_json=json_path,
        raise_on_failure=False,
    )

    assert report["status"] == "FAILED"
    assert report["checks"]["invariants"]["passed"] is False
    assert len(report["checks"]["invariants"]["violations"]) > 0


def test_qa_fails_on_zero_volume_session(valid_sessions_and_trades, tmp_path):
    """Test QA fails when a session has zero volume."""
    sessions_df, clean_df, _ = valid_sessions_and_trades
    corrupted_sessions = sessions_df.copy()
    corrupted_sessions.loc[0, "vol"] = 0
    corrupted_sessions.loc[0, "n_trades"] = 0

    zv_res = check_zero_volume_days(corrupted_sessions)
    assert zv_res["passed"] is False

    report = run_qa(
        sessions_input=corrupted_sessions,
        clean_trades_input=clean_df,
        raise_on_failure=False,
    )

    assert report["status"] == "FAILED"
    assert report["checks"]["zero_volume"]["passed"] is False
    assert len(report["checks"]["zero_volume"]["zero_volume_sessions"]) > 0


def test_qa_fails_on_mixed_instrument_ids(valid_sessions_and_trades):
    """Test QA detects when trades within a session cross different instrument_ids."""
    sessions_df, clean_df, _ = valid_sessions_and_trades
    # Mix instrument IDs in the same session
    corrupted_trades = clean_df.copy()
    corrupted_trades.loc[0, "instrument_id"] = 9999

    res = check_contract_rolls(sessions_df, corrupted_trades)
    assert res["passed"] is False
    assert len(res["mixed_session_trades"]) > 0


def test_qa_detects_volume_outliers():
    """Test QA detects and flags extreme daily volume outliers."""
    # 10 days of normal volume around 1000, 1 day with 100,000
    dates = [f"2026-10-{i:02d}" for i in range(1, 12)]
    volumes = [1000, 1050, 980, 1020, 1010, 990, 1030, 970, 1040, 1000, 100_000]

    rows = []
    for d, v in zip(dates, volumes, strict=True):
        rows.append({
            "trade_date": d,
            "sub": "ny",
            "instrument_id": 1001,
            "o": 2000.0,
            "h": 2005.0,
            "l": 1995.0,
            "c": 2002.0,
            "vol": v,
            "buy_v": v // 2,
            "sell_v": v // 2,
            "n_trades": 100,
            "poc": 2000.0,
            "val": 1998.0,
            "vah": 2002.0,
            "t_start": 1000,
            "t_end": 2000,
        })
    df = pd.DataFrame(rows)
    res = check_daily_volume_outliers(df)
    assert len(res["outliers"]) == 1
    assert res["outliers"][0]["trade_date"] == "2026-10-11"


def test_qa_session_counts():
    """Test session count verification vs 250/yr norm."""
    rows = []
    # 5 trade dates in 2026
    for i in range(1, 6):
        d = f"2026-10-{i:02d}"
        for s in ["asia", "london", "ny"]:
            rows.append({
                "trade_date": d,
                "sub": s,
                "instrument_id": 1001,
                "o": 2000.0,
                "h": 2005.0,
                "l": 1995.0,
                "c": 2002.0,
                "vol": 100,
                "buy_v": 50,
                "sell_v": 50,
                "n_trades": 10,
                "poc": 2000.0,
                "val": 1998.0,
                "vah": 2002.0,
                "t_start": 1000,
                "t_end": 2000,
            })
    df = pd.DataFrame(rows)
    res = check_session_counts(df)
    assert res["passed"] is True
    assert 2026 in res["years"]
    assert res["years"][2026]["trade_dates_count"] == 5


def test_qa_cli_invocation(valid_sessions_and_trades, tmp_path):
    """Test src.qa CLI execution."""
    _, _, sessions_dir = valid_sessions_and_trades
    json_out = tmp_path / "cli_report.json"
    md_out = tmp_path / "cli_report.md"

    cmd = [
        sys.executable,
        "-m",
        "src.qa",
        "--sessions",
        str(sessions_dir),
        "--output-json",
        str(json_out),
        "--output-md",
        str(md_out),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"CLI failed with error: {proc.stderr}"
    assert json_out.exists()
    assert md_out.exists()
