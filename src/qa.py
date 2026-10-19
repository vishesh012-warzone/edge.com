"""Data Quality Assurance (QA) and Pipeline Verification.

Generates comprehensive JSON and Markdown reports validating:
1. Session counts per calendar year vs 250/yr norm.
2. Per-day volume distribution and extreme outliers.
3. Zero-volume days and empty sessions.
4. Profile invariants:
   - VAL <= POC <= VAH
   - POC inside [low, high]
   - Value-area volume >= 70% of total
   - t_start <= t_end
5. Contract roll isolation: levels never cross contract rolls (use instrument_id).

Hard Rules:
- Never fix data silently.
- Exit non-zero if any invariant or critical data integrity check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from src.config import (
    PROCESSED_DATA_DIR,
    TICK_SIZE_GC,
    VALUE_AREA_PCT,
)


def _load_df_or_path(data_input: Path | pd.DataFrame | str | None) -> pd.DataFrame:
    """Load DataFrame from path or return copy if already a DataFrame."""
    if data_input is None:
        return pd.DataFrame()
    if isinstance(data_input, pd.DataFrame):
        return data_input.copy()

    path = Path(data_input)
    if not path.exists():
        return pd.DataFrame()

    if path.is_file():
        return pd.read_parquet(path)

    parquet_files = list(path.glob("**/*.parquet"))
    if not parquet_files:
        return pd.DataFrame()

    con = duckdb.connect()
    return con.execute(f"SELECT * FROM read_parquet('{path}/**/*.parquet')").df()


def check_session_counts(sessions_df: pd.DataFrame) -> dict[str, Any]:
    """Check session counts per year against the ~250 trading days/yr norm.

    Args:
        sessions_df: Sessions DataFrame.

    Returns:
        Dict with year metrics and check results.
    """
    if sessions_df.empty:
        return {
            "passed": True,
            "years": {},
            "total_trade_dates": 0,
            "total_sessions": 0,
            "message": "Empty sessions DataFrame",
        }

    df = sessions_df.copy()
    df["year"] = pd.to_datetime(df["trade_date"]).dt.year

    years_dict: dict[int, Any] = {}
    passed = True

    for year, ydf in df.groupby("year"):
        n_dates = int(ydf["trade_date"].nunique())
        n_total = int(len(ydf))
        sub_counts = ydf["sub"].value_counts().to_dict()

        # Check for missing sub sessions within present trade dates
        trade_date_subs = ydf.groupby("trade_date")["sub"].apply(set)
        dates_missing_sessions = [
            str(td)
            for td, subs in trade_date_subs.items()
            if not {"asia", "london", "ny"}.issubset(subs)
        ]

        # Incomplete or sample years might have fewer dates;
        # complete historical years should be 240-265
        status = "PASSED"
        if n_dates < 200 and year < datetime.now(timezone.utc).year:
            status = "NOTE_PARTIAL_YEAR"

        years_dict[int(year)] = {
            "trade_dates_count": n_dates,
            "expected_norm": 250,
            "total_sessions": n_total,
            "sub_session_counts": sub_counts,
            "dates_missing_sub_sessions": dates_missing_sessions,
            "status": status,
        }

    return {
        "passed": passed,
        "years": years_dict,
        "total_trade_dates": int(df["trade_date"].nunique()),
        "total_sessions": int(len(df)),
    }


def check_daily_volume_outliers(
    sessions_df: pd.DataFrame,
    trades_df: pd.DataFrame | None = None,
    outlier_iqr_multiplier: float = 4.0,
) -> dict[str, Any]:
    """Detect per-day volume outliers and compute volume summary statistics.

    Args:
        sessions_df: Sessions DataFrame.
        trades_df: Optional cleaned trades DataFrame.
        outlier_iqr_multiplier: IQR threshold multiplier (default 4.0).

    Returns:
        Dict containing distribution stats and detected outlier days.
    """
    if sessions_df.empty:
        return {
            "passed": True,
            "outliers": [],
            "stats": {},
        }

    daily_vol = sessions_df.groupby("trade_date")["vol"].sum().astype(float)
    if daily_vol.empty:
        return {"passed": True, "outliers": [], "stats": {}}

    q25 = float(np.percentile(daily_vol, 25))
    q75 = float(np.percentile(daily_vol, 75))
    iqr = q75 - q25
    med = float(np.median(daily_vol))
    mean = float(np.mean(daily_vol))
    std = float(np.std(daily_vol)) if len(daily_vol) > 1 else 0.0

    lower_bound = max(0.0, med - outlier_iqr_multiplier * iqr)
    upper_bound = med + outlier_iqr_multiplier * iqr

    outliers = []
    for td, v in daily_vol.items():
        if (v > upper_bound or v < lower_bound) and iqr > 0:
            z_score = float((v - mean) / std) if std > 0 else 0.0
            outliers.append({
                "trade_date": str(td),
                "volume": int(v),
                "median": med,
                "z_score": round(z_score, 2),
                "type": "HIGH" if v > upper_bound else "LOW",
            })

    return {
        "passed": True,
        "stats": {
            "mean": round(mean, 1),
            "std": round(std, 1),
            "median": round(med, 1),
            "iqr": round(iqr, 1),
            "min": int(daily_vol.min()),
            "max": int(daily_vol.max()),
        },
        "outliers": outliers,
    }


def check_zero_volume_days(sessions_df: pd.DataFrame) -> dict[str, Any]:
    """Verify there are no zero-volume active sessions or trade dates.

    Args:
        sessions_df: Sessions DataFrame.

    Returns:
        Dict with zero-volume inspection results.
    """
    if sessions_df.empty:
        return {"passed": True, "zero_volume_sessions": []}

    zero_rows = sessions_df[
        (sessions_df["vol"] <= 0) | (sessions_df["n_trades"] <= 0)
    ]

    zero_sessions = []
    for _, row in zero_rows.iterrows():
        zero_sessions.append({
            "trade_date": str(row["trade_date"]),
            "sub": str(row["sub"]),
            "instrument_id": int(row["instrument_id"]),
            "vol": int(row["vol"]),
            "n_trades": int(row["n_trades"]),
        })

    passed = len(zero_sessions) == 0
    return {
        "passed": passed,
        "zero_volume_sessions": zero_sessions,
        "violation_count": len(zero_sessions),
    }


def check_profile_invariants(
    sessions_df: pd.DataFrame,
    trades_df: pd.DataFrame | None = None,
    tick_size: float = TICK_SIZE_GC,
    va_target_pct: float = VALUE_AREA_PCT,
) -> dict[str, Any]:
    """Verify profile invariants across all sessions.

    Invariants:
    1. VAL <= POC <= VAH
    2. POC inside [l, h]
    3. VAL inside [l, h] and VAH inside [l, h]
    4. t_start <= t_end
    5. vol >= buy_v + sell_v
    6. Value Area volume >= 70% of session volume (when trades are available).

    Args:
        sessions_df: Sessions DataFrame.
        trades_df: Optional clean trades DataFrame.
        tick_size: Discretization unit.
        va_target_pct: Target Value Area fraction (0.70).

    Returns:
        Dict with invariant validation results.
    """
    if sessions_df.empty:
        return {"passed": True, "violations": []}

    violations = []

    for idx, row in sessions_df.iterrows():
        td = str(row["trade_date"])
        sub = str(row["sub"])
        inst_id = int(row["instrument_id"])
        val = float(row["val"])
        poc = float(row["poc"])
        vah = float(row["vah"])
        low = float(row["l"])
        high = float(row["h"])
        t_start = int(row["t_start"])
        t_end = int(row["t_end"])
        vol = int(row["vol"])
        buy_v = int(row["buy_v"])
        sell_v = int(row["sell_v"])

        if val > poc:
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "VAL <= POC",
                "detail": f"VAL ({val}) > POC ({poc})",
            })
        if poc > vah:
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "POC <= VAH",
                "detail": f"POC ({poc}) > VAH ({vah})",
            })
        if not (low <= poc <= high):
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "low <= POC <= high",
                "detail": f"POC ({poc}) outside [{low}, {high}]",
            })
        if not (low <= val <= high):
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "low <= VAL <= high",
                "detail": f"VAL ({val}) outside [{low}, {high}]",
            })
        if not (low <= vah <= high):
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "low <= VAH <= high",
                "detail": f"VAH ({vah}) outside [{low}, {high}]",
            })
        if t_start > t_end:
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "t_start <= t_end",
                "detail": f"t_start ({t_start}) > t_end ({t_end})",
            })
        if vol < buy_v + sell_v:
            violations.append({
                "row": int(idx),
                "trade_date": td,
                "sub": sub,
                "instrument_id": inst_id,
                "rule": "vol >= buy_v + sell_v",
                "detail": f"vol ({vol}) < buy_v ({buy_v}) + sell_v ({sell_v})",
            })

    # If trades are available, verify value-area volume >= 70%
    if trades_df is not None and not trades_df.empty:
        valid_trades = trades_df[trades_df["sub"].notna()]
        for _, s_row in sessions_df.iterrows():
            td = s_row["trade_date"]
            sub = s_row["sub"]
            inst_id = s_row["instrument_id"]
            val = float(s_row["val"])
            vah = float(s_row["vah"])
            vol = float(s_row["vol"])

            mask = (
                (valid_trades["trade_date"] == td)
                & (valid_trades["sub"] == sub)
                & (valid_trades["instrument_id"] == inst_id)
            )
            sub_trades = valid_trades[mask]
            if not sub_trades.empty and vol > 0:
                va_trades = sub_trades[
                    (sub_trades["price"] >= val - 1e-6)
                    & (sub_trades["price"] <= vah + 1e-6)
                ]
                va_vol = float(va_trades["size"].sum())
                if va_vol < (va_target_pct * vol - 1e-6):
                    violations.append({
                        "trade_date": str(td),
                        "sub": str(sub),
                        "instrument_id": int(inst_id),
                        "rule": f"VA volume >= {int(va_target_pct * 100)}% of total",
                        "detail": f"VA volume ({va_vol}) < {va_target_pct * vol}",
                    })

    passed = len(violations) == 0
    return {
        "passed": passed,
        "violations": violations,
        "total_checks": len(sessions_df) * 7,
    }


def check_contract_rolls(
    sessions_df: pd.DataFrame,
    trades_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Validate that levels never cross a contract roll.

    Ensures:
    1. Every session row has a non-null, valid instrument_id.
    2. Trades within each session are 100% homogenous in instrument_id.
    3. Contract rolls are detected and logged without level cross-contamination.

    Args:
        sessions_df: Sessions DataFrame.
        trades_df: Optional clean trades DataFrame.

    Returns:
        Dict with roll validation results.
    """
    if sessions_df.empty:
        return {"passed": True, "rolls_detected": [], "mixed_session_trades": []}

    # Invariant 1: Valid instrument_id
    invalid_inst_rows = sessions_df[sessions_df["instrument_id"].isna()]
    if not invalid_inst_rows.empty:
        return {
            "passed": False,
            "error": "Null instrument_id found in sessions table",
            "invalid_rows": len(invalid_inst_rows),
        }

    mixed_session_trades = []
    # Invariant 2: If trades_df provided, ensure no session mixes instrument_ids
    if trades_df is not None and not trades_df.empty:
        valid_trades = trades_df[trades_df["sub"].notna()]
        grouped = valid_trades.groupby(["trade_date", "sub"])["instrument_id"].nunique()
        mixed = grouped[grouped > 1]
        for (td, sub), cnt in mixed.items():
            mixed_session_trades.append({
                "trade_date": str(td),
                "sub": str(sub),
                "instrument_count": int(cnt),
                "error": "Trades within session have multiple instrument_ids",
            })

    # Track roll transitions across consecutive sessions
    inst_ids = sessions_df["instrument_id"].to_numpy()
    trade_dates = sessions_df["trade_date"].to_numpy()
    subs = sessions_df["sub"].to_numpy()

    rolls_detected = []
    for i in range(1, len(inst_ids)):
        if inst_ids[i] != inst_ids[i - 1]:
            rolls_detected.append({
                "from_date": str(trade_dates[i - 1]),
                "from_sub": str(subs[i - 1]),
                "from_instrument_id": int(inst_ids[i - 1]),
                "to_date": str(trade_dates[i]),
                "to_sub": str(subs[i]),
                "to_instrument_id": int(inst_ids[i]),
            })

    passed = len(mixed_session_trades) == 0
    return {
        "passed": passed,
        "rolls_detected": rolls_detected,
        "mixed_session_trades": mixed_session_trades,
    }


def generate_markdown_report(report_data: dict[str, Any]) -> str:
    """Format QA results into a clean GitHub-style Markdown report."""
    status = report_data.get("status", "UNKNOWN")
    ts = report_data.get("timestamp_utc", "")
    summary = report_data.get("summary", {})
    checks = report_data.get("checks", {})

    status_badge = "PASSED" if status == "PASSED" else "FAILED"
    alert_type = "NOTE" if status == "PASSED" else "CAUTION"

    total_sess = summary.get("total_sessions", 0)
    total_dates = summary.get("total_trade_dates", 0)
    md = [
        "# Data Quality Assurance (QA) Report\n",
        f"> [!{alert_type}]",
        f"> **Status**: **{status_badge}** | **Evaluated at**: `{ts}`",
        f"> Total Sessions: `{total_sess}` | Unique Trade Dates: `{total_dates}`\n",
        "## Summary of Integrity Checks\n",
        "| Check Category | Result | Details |",
        "| :--- | :--- | :--- |",
    ]

    sc = checks.get("session_counts", {})
    sc_res = "PASS" if sc.get("passed") else "FAIL"
    n_years = len(sc.get("years", {}))
    md.append(f"| Session Counts (vs 250/yr norm) | {sc_res} | {n_years} years evaluated |")

    vo = checks.get("daily_volume_outliers", {})
    n_outliers = len(vo.get("outliers", []))
    md.append(f"| Daily Volume Outliers | PASS | {n_outliers} outliers flagged |")

    zv = checks.get("zero_volume_days", {})
    zv_res = "PASS" if zv.get("passed") else "FAIL"
    n_zv = len(zv.get("zero_volume_sessions", []))
    md.append(f"| Zero-Volume Days / Empty Sessions | {zv_res} | {n_zv} zero-volume sessions |")

    inv = checks.get("invariants", {})
    inv_res = "PASS" if inv.get("passed") else "FAIL"
    n_inv = len(inv.get("violations", []))
    md.append(f"| Level Invariants (VAL <= POC <= VAH) | {inv_res} | {n_inv} violations |")

    cr = checks.get("contract_rolls", {})
    cr_res = "PASS" if cr.get("passed") else "FAIL"
    n_cr = len(cr.get("rolls_detected", []))
    md.append(f"| Contract Roll Isolation | {cr_res} | {n_cr} roll transitions detected |")

    # Invariants details
    if not inv.get("passed", True):
        md.extend([
            "\n### Invariant Violations\n",
            "| Trade Date | Sub | Instrument ID | Rule Broken | Detail |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for v in inv.get("violations", []):
            td_val = v.get("trade_date")
            sub_val = v.get("sub")
            inst_val = v.get("instrument_id")
            rule_val = v.get("rule")
            det_val = v.get("detail")
            md.append(f"| {td_val} | {sub_val} | {inst_val} | `{rule_val}` | {det_val} |")

    # Zero volume details
    if not zv.get("passed", True):
        md.extend([
            "\n### Zero-Volume Sessions\n",
            "| Trade Date | Sub | Instrument ID | Volume | Trades |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for z in zv.get("zero_volume_sessions", []):
            td_val = z.get("trade_date")
            sub_val = z.get("sub")
            inst_val = z.get("instrument_id")
            vol_val = z.get("vol")
            trades_val = z.get("n_trades")
            md.append(f"| {td_val} | {sub_val} | {inst_val} | {vol_val} | {trades_val} |")

    # Volume outliers details
    if vo.get("outliers"):
        md.extend([
            "\n### Daily Volume Outliers\n",
            "| Trade Date | Volume | Median | Z-Score | Direction |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for o in vo.get("outliers", []):
            td_val = o.get("trade_date")
            vol_val = o.get("volume")
            med_val = o.get("median")
            z_val = o.get("z_score")
            type_val = o.get("type")
            md.append(f"| {td_val} | {vol_val} | {med_val} | {z_val} | {type_val} |")

    return "\n".join(md) + "\n"


def run_qa(
    sessions_input: Path | pd.DataFrame | str = PROCESSED_DATA_DIR / "sessions",
    clean_trades_input: Path | pd.DataFrame | str | None = None,
    output_json: Path | str | None = None,
    output_md: Path | str | None = None,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    """Execute all QA checks, save reports, and optionally exit non-zero on failure.

    Args:
        sessions_input: Path or DataFrame of sessions table.
        clean_trades_input: Path or DataFrame of cleaned trades table.
        output_json: Path to write JSON report.
        output_md: Path to write Markdown report.
        raise_on_failure: If True, exits process with exit code 1 on failure.

    Returns:
        Report dictionary.
    """
    sessions_df = _load_df_or_path(sessions_input)
    trades_df = _load_df_or_path(clean_trades_input)

    c_session_counts = check_session_counts(sessions_df)
    c_volume_outliers = check_daily_volume_outliers(sessions_df, trades_df)
    c_zero_volume = check_zero_volume_days(sessions_df)
    c_invariants = check_profile_invariants(sessions_df, trades_df)
    c_contract_rolls = check_contract_rolls(sessions_df, trades_df)

    overall_passed = (
        c_session_counts.get("passed", True)
        and c_zero_volume.get("passed", True)
        and c_invariants.get("passed", True)
        and c_contract_rolls.get("passed", True)
    )

    total_unique_dates = (
        int(sessions_df["trade_date"].nunique()) if not sessions_df.empty else 0
    )
    report_data = {
        "status": "PASSED" if overall_passed else "FAILED",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_sessions": len(sessions_df),
            "total_trade_dates": total_unique_dates,
            "overall_passed": overall_passed,
        },
        "checks": {
            "session_counts": c_session_counts,
            "daily_volume_outliers": c_volume_outliers,
            "zero_volume": c_zero_volume,
            "invariants": c_invariants,
            "contract_rolls": c_contract_rolls,
        },
    }

    # Save JSON report if requested
    if output_json:
        json_path = Path(output_json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

    # Save Markdown report if requested
    if output_md:
        md_content = generate_markdown_report(report_data)
        md_path = Path(output_md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

    if raise_on_failure and not overall_passed:
        sys.stderr.write(f"QA Validation FAILED: {report_data['checks']}\n")
        sys.exit(1)

    return report_data


def main() -> None:
    """CLI entry point for QA validation."""
    parser = argparse.ArgumentParser(description="Run QA integrity checks on sessions and trades.")
    parser.add_argument(
        "--sessions",
        type=str,
        default=str(PROCESSED_DATA_DIR / "sessions"),
        help="Path to sessions table",
    )
    parser.add_argument(
        "--trades",
        type=str,
        default=None,
        help="Path to clean trades table",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="reports/qa_report.json",
        help="Path to output JSON report",
    )
    parser.add_argument(
        "--output-md",
        type=str,
        default="reports/qa_report.md",
        help="Path to output Markdown report",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="Exit non-zero if any check fails",
    )

    args = parser.parse_args()

    run_qa(
        sessions_input=args.sessions,
        clean_trades_input=args.trades,
        output_json=args.output_json,
        output_md=args.output_md,
        raise_on_failure=args.strict,
    )


if __name__ == "__main__":
    main()
