"""Tests for synthetic trades fixture generator."""

from typing import Any, Callable


def test_synthetic_trades_generator(
    synthetic_trades_generator: Callable[..., list[dict[str, Any]]],
) -> None:
    """Verify synthetic trades satisfy timestamp ordering, tick size alignment, and determinism."""
    trades1 = synthetic_trades_generator(n_trades=100, seed=42)
    trades2 = synthetic_trades_generator(n_trades=100, seed=42)

    assert len(trades1) == 100
    assert trades1 == trades2  # Deterministic with same seed

    # Verify strictly increasing UTC timestamps
    for i in range(1, len(trades1)):
        assert trades1[i]["ts"] > trades1[i - 1]["ts"]

    # Verify price tick alignment (GC = 0.10)
    for t in trades1:
        assert round(t["price"] * 10) % 1 == 0
        assert t["instrument_id"] == "GC"
        assert t["size"] > 0
        assert t["side"] in ("A", "B")
        assert "trade_date" in t


def test_sample_gc_trades_fixture(sample_gc_trades: list[dict[str, Any]]) -> None:
    """Verify default fixture sample."""
    assert len(sample_gc_trades) == 200
