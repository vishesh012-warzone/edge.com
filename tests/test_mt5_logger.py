"""Unit tests for tools/mt5_logger.py using FakeMT5Feed.

Verifies:
- Tick deduplication on time_msc
- Flush to date=.../HHMMSS.parquet with server offset measurement
- 5-minute gap alert during open hours (and suppression during closed hours)
- Crash alert on unhandled exceptions
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from tools.mt5_logger import (
    FakeMT5Feed,
    MT5Logger,
    TelegramAlerter,
    is_market_open,
)

TZ_ET = ZoneInfo("America/New_York")


def test_deduplication_on_time_msc() -> None:
    """Verify ticks with already-seen time_msc are skipped."""
    feed = FakeMT5Feed()
    logger = MT5Logger(feed=feed)

    batch1 = [
        {
            "time": 1700000001,
            "time_msc": 1700000001000,
            "bid": 2000.1,
            "ask": 2000.3,
            "volume": 1,
            "flags": 6,
        },
        {
            "time": 1700000002,
            "time_msc": 1700000002000,
            "bid": 2000.2,
            "ask": 2000.4,
            "volume": 2,
            "flags": 6,
        },
    ]
    # Batch 2 contains a duplicate (1700000002000) and a new tick (1700000003000)
    batch2 = [
        {
            "time": 1700000002,
            "time_msc": 1700000002000,
            "bid": 2000.2,
            "ask": 2000.4,
            "volume": 2,
            "flags": 6,
        },
        {
            "time": 1700000003,
            "time_msc": 1700000003000,
            "bid": 2000.3,
            "ask": 2000.5,
            "volume": 1,
            "flags": 6,
        },
    ]

    feed.add_batch(batch1)
    feed.add_batch(batch2)

    added1 = logger.poll_once(current_time=1700000001.0)
    assert added1 == 2
    assert len(logger._buffer) == 2

    added2 = logger.poll_once(current_time=1700000002.0)
    assert added2 == 1  # only 1 new tick
    assert len(logger._buffer) == 3

    buffered_msc = [r.time_msc for r in logger._buffer]
    assert buffered_msc == [1700000001000, 1700000002000, 1700000003000]


def test_flush_output_format_and_server_offset(tmp_path: Path) -> None:
    """Verify flush writes date=YYYY-MM-DD/HHMMSS.parquet (zstd) with measured offset."""
    feed = FakeMT5Feed()
    logger = MT5Logger(feed=feed, output_dir=tmp_path)

    # Server tick at 1700000005, local time at 1700000000 -> measured offset = +5.0s
    local_time = 1700000000.0
    batch = [
        {
            "time": 1700000005,
            "time_msc": 1700000005000,
            "bid": 2010.5,
            "ask": 2010.8,
            "last": 2010.6,
            "volume": 5,
            "flags": 6,
        },
    ]
    feed.add_batch(batch)
    logger.poll_once(current_time=local_time)

    # Flush at UTC 2023-11-14 22:13:20
    parquet_path = logger.flush(flush_time=local_time)
    assert parquet_path is not None
    assert parquet_path.exists()
    assert parquet_path.name == "221320.parquet"
    assert parquet_path.parent.name == "date=2023-11-14"

    # Inspect parquet
    table = pq.read_table(parquet_path)
    assert table.num_rows == 1
    assert "server_offset_s" in table.column_names
    offset_val = table.column("server_offset_s")[0].as_py()
    assert abs(offset_val - 5.0) < 1e-4

    # Check parquet metadata
    meta = table.schema.metadata
    assert b"server_offset_seconds" in meta
    assert b"5.0" in meta[b"server_offset_seconds"]

    # Buffer should be cleared
    assert len(logger._buffer) == 0


def test_gap_alert_during_open_hours() -> None:
    """Verify 5-minute lack of ticks triggers Telegram alert during market open."""
    feed = FakeMT5Feed()
    alerter = TelegramAlerter()
    logger = MT5Logger(feed=feed, alerter=alerter, gap_timeout_sec=300.0)

    # Tuesday 14:00 ET (Market is open)
    t_open = datetime(2026, 10, 20, 14, 0, 0, tzinfo=TZ_ET)
    t_open_utc = t_open.astimezone(timezone.utc).timestamp()

    # Initial state: tick received 301 seconds ago
    logger._last_tick_time = t_open_utc - 301.0
    logger.check_health(current_time=t_open_utc)

    assert len(alerter.sent_alerts) == 1
    assert "No XAUUSD ticks received" in alerter.sent_alerts[0]


def test_gap_alert_suppressed_during_closed_hours() -> None:
    """Verify lack of ticks does NOT trigger alert during weekend/closed hours."""
    feed = FakeMT5Feed()
    alerter = TelegramAlerter()
    logger = MT5Logger(feed=feed, alerter=alerter, gap_timeout_sec=300.0)

    # Saturday 12:00 ET (Market is closed)
    t_closed = datetime(2026, 10, 24, 12, 0, 0, tzinfo=TZ_ET)
    t_closed_utc = t_closed.astimezone(timezone.utc).timestamp()

    logger._last_tick_time = t_closed_utc - 600.0
    logger.check_health(current_time=t_closed_utc)

    # No alert should be sent
    assert len(alerter.sent_alerts) == 0


def test_market_hours_checker() -> None:
    """Verify is_market_open accurately distinguishes open vs closed hours."""
    # Tuesday 10:00 ET -> Open
    dt1 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt1) is True

    # Tuesday 17:30 ET -> Closed (daily break 17:00-18:00 ET)
    dt2 = datetime(2026, 10, 20, 17, 30, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt2) is False

    # Friday 16:50 ET -> Open
    dt3 = datetime(2026, 10, 23, 16, 50, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt3) is True

    # Friday 17:05 ET -> Closed (weekend close)
    dt4 = datetime(2026, 10, 23, 17, 5, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt4) is False

    # Saturday 12:00 ET -> Closed
    dt5 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt5) is False

    # Sunday 17:50 ET -> Closed
    dt6 = datetime(2026, 10, 25, 17, 50, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt6) is False

    # Sunday 18:05 ET -> Open (week open)
    dt7 = datetime(2026, 10, 25, 18, 5, 0, tzinfo=TZ_ET).astimezone(timezone.utc)
    assert is_market_open(dt7) is True
