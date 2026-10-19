"""Pytest fixtures for unit tests.

Provides synthetic trade generators for testing pipelines without accessing immutable raw data.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from src.config import DEFAULT_SEED, TICK_SIZE_GC, compute_trade_date


@pytest.fixture
def synthetic_trades_generator() -> Callable[..., list[dict[str, Any]]]:
    """Factory fixture generating synthetic CME GC trades.

    Yields a generator function parameterized by trade count, seed, start timestamp, etc.
    All timestamps are generated in UTC (ns) and prices are snapped to GC tick size (0.10).
    """

    def _generate(
        n_trades: int = 500,
        start_utc: datetime | None = None,
        base_price: float = 2650.0,
        tick_size: float = TICK_SIZE_GC,
        seed: int = DEFAULT_SEED,
        instrument_id: str = "GC",
    ) -> list[dict[str, Any]]:
        rng = random.Random(seed)
        start_dt = start_utc or datetime(2026, 10, 19, 14, 0, 0, tzinfo=timezone.utc)
        current_ts_ns = int(start_dt.timestamp() * 1e9)
        current_price = base_price

        trades: list[dict[str, Any]] = []

        for _ in range(n_trades):
            # Time increments between 5ms and 200ms
            dt_ns = rng.randint(5_000_000, 200_000_000)
            current_ts_ns += dt_ns

            # Random walk in ticks (-2, -1, 0, 1, 2)
            tick_move = rng.choice([-2, -1, 0, 0, 0, 1, 2])
            current_price = round(current_price + tick_move * tick_size, 2)

            size = rng.choice([1, 1, 1, 2, 2, 5, 10])
            # A = aggressive buy (Ask hit), B = aggressive sell (Bid hit)
            side = rng.choice(["A", "B"])

            t_date = compute_trade_date(current_ts_ns)

            trades.append(
                {
                    "ts": current_ts_ns,
                    "instrument_id": instrument_id,
                    "price": current_price,
                    "size": size,
                    "side": side,
                    "trade_date": t_date.isoformat(),
                }
            )

        return trades

    return _generate


@pytest.fixture
def sample_gc_trades(
    synthetic_trades_generator: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Default sample of 200 synthetic GC trades."""
    return synthetic_trades_generator(n_trades=200, seed=DEFAULT_SEED)
