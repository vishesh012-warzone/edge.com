"""MetaTrader 5 live tick logging daemon for XAUUSD.

Polls MT5 copy_ticks_from every 250 ms, dedupes on time_msc, records
bid/ask/last/volume/flags, measures server offset (tick.time - time.time()) per flush,
and flushes every 10 minutes (and on SIGINT) to:
    data/raw_mt5/xauusd/date=YYYY-MM-DD/HHMMSS.parquet (zstd)

Sends Telegram alerts if no ticks are received for >= 5 minutes during market hours
or upon an unhandled crash. MT5 is abstracted behind MT5FeedProtocol.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# --- Default Constants ---
DEFAULT_SYMBOL: str = "XAUUSD"
DEFAULT_POLL_INTERVAL_SEC: float = 0.250  # 250 ms
DEFAULT_FLUSH_INTERVAL_SEC: float = 600.0  # 10 minutes
DEFAULT_GAP_TIMEOUT_SEC: float = 300.0  # 5 minutes
DEFAULT_RAW_MT5_DIR: Path = Path("data/raw_mt5")

TZ_UTC: ZoneInfo = ZoneInfo("UTC")
TZ_NEW_YORK: ZoneInfo = ZoneInfo("America/New_York")


# --- MT5 Feed Interface ---
class MT5FeedProtocol(Protocol):
    """Abstract interface for MT5 tick feed."""

    def initialize(self) -> bool:
        """Initialize connection to terminal."""
        ...

    def shutdown(self) -> None:
        """Shutdown connection to terminal."""
        ...

    def copy_ticks_from(
        self, symbol: str, date_from: datetime | int, count: int, flags: int
    ) -> np.ndarray | None:
        """Copy ticks from MT5 starting from a timestamp or index."""
        ...


class MetaTrader5Client:
    """Production client wrapping MetaTrader5 Windows SDK."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5

            self._mt5 = mt5
        except ImportError as exc:
            raise RuntimeError(
                "MetaTrader5 package is not available. Ensure you are on Windows "
                "with MetaTrader5 installed: pip install MetaTrader5"
            ) from exc

    def initialize(self) -> bool:
        return bool(self._mt5.initialize())

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def copy_ticks_from(
        self, symbol: str, date_from: datetime | int, count: int, flags: int
    ) -> np.ndarray | None:
        ticks = self._mt5.copy_ticks_from(symbol, date_from, count, flags)
        return ticks


class FakeMT5Feed:
    """Mock feed for testing and non-Windows simulation."""

    def __init__(self, tick_batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.tick_batches: list[list[dict[str, Any]]] = tick_batches or []
        self._call_count: int = 0
        self.initialized: bool = False

    def initialize(self) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def add_batch(self, ticks: list[dict[str, Any]]) -> None:
        self.tick_batches.append(ticks)

    def copy_ticks_from(
        self, symbol: str, date_from: datetime | int, count: int, flags: int
    ) -> np.ndarray | None:
        if self._call_count >= len(self.tick_batches):
            return None

        batch = self.tick_batches[self._call_count]
        self._call_count += 1

        if not batch:
            return None

        # Convert list of dicts to numpy structured array matching MT5 tick format
        dtype = [
            ("time", "i8"),
            ("bid", "f8"),
            ("ask", "f8"),
            ("last", "f8"),
            ("volume", "u8"),
            ("time_msc", "i8"),
            ("flags", "u4"),
            ("volume_real", "f8"),
        ]
        records = [
            (
                t.get("time", int(t.get("time_msc", 0) / 1000)),
                float(t.get("bid", 0.0)),
                float(t.get("ask", 0.0)),
                float(t.get("last", 0.0)),
                int(t.get("volume", 0)),
                int(t.get("time_msc", 0)),
                int(t.get("flags", 0)),
                float(t.get("volume_real", 0.0)),
            )
            for t in batch
        ]
        return np.array(records, dtype=dtype)


# --- Market Open Hours Validation ---
def is_market_open(dt_utc: datetime) -> bool:
    """Check if spot gold (XAUUSD) market is open.

    Market hours in America/New_York:
    - Open: Sunday 18:00 ET through Friday 17:00 ET.
    - Daily Maintenance Break: Monday-Thursday 17:00-18:00 ET.
    - Weekend Closed: Friday 17:00 ET through Sunday 18:00 ET.
    """
    dt_ny = dt_utc.astimezone(TZ_NEW_YORK)
    weekday = dt_ny.weekday()  # Monday is 0, Sunday is 6
    hour = dt_ny.hour
    minute = dt_ny.minute

    # Friday after 17:00 ET -> Closed
    if weekday == 4 and (hour > 17 or (hour == 17 and minute > 0)):
        return False

    # Saturday all day -> Closed
    if weekday == 5:
        return False

    # Sunday before 18:00 ET -> Closed
    if weekday == 6 and hour < 18:
        return False

    # Monday through Thursday: daily break 17:00 to 18:00 ET -> Closed
    if 0 <= weekday <= 3 and hour == 17:
        return False

    return True


# --- Telegram Alerting ---
class TelegramAlerter:
    """Sends operational alerts via Telegram Bot API."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        post_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self._post_fn = post_fn or self._default_post
        self.sent_alerts: list[str] = []

    def _default_post(self, url: str, payload: dict[str, Any]) -> Any:
        import json
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def send_alert(self, message: str) -> bool:
        """Send message to configured Telegram chat."""
        self.sent_alerts.append(message)
        if not self.bot_token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message}
        try:
            self._post_fn(url, payload)
            return True
        except Exception:
            return False

    def alert_gap(self, gap_seconds: float) -> bool:
        msg = f"[MT5 WARNING] No XAUUSD ticks received for {gap_seconds:.1f}s during market hours!"
        return self.send_alert(msg)

    def alert_crash(self, exc_traceback: str) -> bool:
        msg = f"[MT5 CRITICAL] Logger crashed:\n{exc_traceback[:3000]}"
        return self.send_alert(msg)


# --- Core Tick Buffer & Daemon ---
@dataclass
class TickRecord:
    time_msc: int
    bid: float
    ask: float
    last: float
    volume: float
    flags: int
    server_offset_s: float


@dataclass
class MT5Logger:
    """Manages high-frequency MT5 polling, deduplication, and scheduled parquet flushes."""

    feed: MT5FeedProtocol
    symbol: str = DEFAULT_SYMBOL
    output_dir: Path = DEFAULT_RAW_MT5_DIR
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
    flush_interval_sec: float = DEFAULT_FLUSH_INTERVAL_SEC
    gap_timeout_sec: float = DEFAULT_GAP_TIMEOUT_SEC
    alerter: TelegramAlerter = field(default_factory=TelegramAlerter)

    # State tracking
    _buffer: list[TickRecord] = field(default_factory=list)
    _seen_msc: set[int] = field(default_factory=set)
    _last_flush_time: float = field(default_factory=time.time)
    _last_tick_time: float = field(default_factory=time.time)
    _last_server_offset: float = 0.0
    _gap_alerted: bool = False
    _running: bool = False

    def record_ticks(self, raw_ticks: np.ndarray, current_time: float) -> int:
        """Deduplicate and append incoming ticks to buffer.

        Returns:
            Count of new unique ticks appended.
        """
        if raw_ticks is None or len(raw_ticks) == 0:
            return 0

        new_count = 0
        offsets: list[float] = []

        for tick in raw_ticks:
            t_msc = int(tick["time_msc"])
            if t_msc in self._seen_msc:
                continue

            # Measured server offset = tick.time (server seconds) - local time.time()
            t_sec = float(tick["time"])
            offset_s = t_sec - current_time
            offsets.append(offset_s)

            record = TickRecord(
                time_msc=t_msc,
                bid=float(tick["bid"]),
                ask=float(tick["ask"]),
                last=float(tick["last"]),
                volume=float(tick["volume"]),
                flags=int(tick["flags"]),
                server_offset_s=offset_s,
            )
            self._buffer.append(record)
            self._seen_msc.add(t_msc)
            new_count += 1

        if offsets:
            self._last_server_offset = float(np.mean(offsets))
            self._last_tick_time = current_time
            self._gap_alerted = False

        # Bound seen_msc cache memory
        if len(self._seen_msc) > 500_000:
            # Retain the most recent 100,000 entries
            recent = sorted(self._seen_msc)[-100_000:]
            self._seen_msc = set(recent)

        return new_count

    def flush(self, flush_time: float | None = None) -> Path | None:
        """Write buffered ticks to zstd-compressed Parquet.

        Path: data/raw_mt5/xauusd/date=YYYY-MM-DD/HHMMSS.parquet
        """
        now_ts = flush_time or time.time()
        self._last_flush_time = now_ts

        if not self._buffer:
            return None

        dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        date_str = dt_utc.strftime("%Y-%m-%d")
        time_str = dt_utc.strftime("%H%M%S")

        partition_dir = self.output_dir / self.symbol.lower() / f"date={date_str}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        file_path = partition_dir / f"{time_str}.parquet"

        # Construct Arrow table
        data_dict = {
            "time_msc": [r.time_msc for r in self._buffer],
            "bid": [r.bid for r in self._buffer],
            "ask": [r.ask for r in self._buffer],
            "last": [r.last for r in self._buffer],
            "volume": [r.volume for r in self._buffer],
            "flags": [r.flags for r in self._buffer],
            "server_offset_s": [r.server_offset_s for r in self._buffer],
        }

        metadata = {
            b"server_offset_seconds": str(self._last_server_offset).encode("utf-8"),
            b"symbol": self.symbol.encode("utf-8"),
            b"flush_time_utc": dt_utc.isoformat().encode("utf-8"),
        }

        table = pa.Table.from_pydict(data_dict)
        existing_meta = table.schema.metadata or {}
        combined_meta = {**existing_meta, **metadata}
        table = table.replace_schema_metadata(combined_meta)

        pq.write_table(table, file_path, compression="zstd")

        self._buffer.clear()
        return file_path

    def check_health(self, current_time: float) -> None:
        """Check for gap in tick reception during open market hours."""
        gap_sec = current_time - self._last_tick_time
        dt_utc = datetime.fromtimestamp(current_time, tz=timezone.utc)

        if gap_sec >= self.gap_timeout_sec and is_market_open(dt_utc) and not self._gap_alerted:
            self.alerter.alert_gap(gap_sec)
            self._gap_alerted = True

    def poll_once(self, current_time: float) -> int:
        """Single poll step executing tick fetch, buffer, flush check, and health monitor."""
        # Query last 2 seconds window
        start_ts = int((current_time - 2.0) * 1000)

        raw = self.feed.copy_ticks_from(
            self.symbol,
            start_ts,
            count=1000,
            flags=-1,
        )
        new_ticks = self.record_ticks(raw, current_time)

        # Check flush schedule
        if (current_time - self._last_flush_time) >= self.flush_interval_sec:
            self.flush(current_time)

        # Check tick gap health during market hours
        self.check_health(current_time)

        return new_ticks

    def run(self) -> None:
        """Main daemon loop."""
        if not self.feed.initialize():
            raise RuntimeError("Failed to initialize MT5 feed connection.")

        self._running = True
        self._last_flush_time = time.time()
        self._last_tick_time = time.time()

        def _handle_signal(signum: int, frame: Any) -> None:
            print(f"\n[MT5 LOGGER] Received signal {signum}. Flushing buffer and exiting...")
            self._running = False

        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_signal)

        print(f"[MT5 LOGGER] Logging started for {self.symbol}...")
        try:
            while self._running:
                now = time.time()
                self.poll_once(now)
                time.sleep(self.poll_interval_sec)
        except Exception:
            err = traceback.format_exc()
            print(f"[MT5 LOGGER ERROR] Exception encountered:\n{err}", file=sys.stderr)
            self.alerter.alert_crash(err)
            raise
        finally:
            print("[MT5 LOGGER] Performing final flush before shutdown...")
            self.flush()
            self.feed.shutdown()
            print("[MT5 LOGGER] Shutdown complete.")


def main() -> None:
    """CLI runner for MT5 tick logging daemon."""
    parser = argparse.ArgumentParser(description="MetaTrader 5 live tick logging daemon.")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Symbol to log (default: XAUUSD)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_RAW_MT5_DIR,
        help="Base directory for raw MT5 parquet files.",
    )
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=250,
        help="Polling interval in milliseconds (default: 250).",
    )
    parser.add_argument(
        "--flush-minutes",
        type=int,
        default=10,
        help="Flush interval in minutes (default: 10).",
    )

    args = parser.parse_args()

    client = MetaTrader5Client()
    logger = MT5Logger(
        feed=client,
        symbol=args.symbol,
        output_dir=args.output_dir,
        poll_interval_sec=args.poll_ms / 1000.0,
        flush_interval_sec=args.flush_minutes * 60.0,
    )
    logger.run()


if __name__ == "__main__":
    main()
