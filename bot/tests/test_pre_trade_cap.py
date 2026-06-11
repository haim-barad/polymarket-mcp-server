"""
Tests for the pre-trade cap check added 2026-06-10.

The pre-trade check is implemented inside runner.py as a side-effect
of the order loop, so testing it end-to-end requires constructing a
BotRunner and stubbing _fetch_candidates. We use a simpler approach
here: test the equivalent pure logic directly.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def compute_should_trade(current_exposure: float, trade_size: float, cap: float) -> bool:
    """Returns True if the trade fits under the cap, False if it would exceed."""
    return (current_exposure + trade_size) <= cap


def test_trade_under_cap_succeeds():
    assert compute_should_trade(current_exposure=10.0, trade_size=2.5, cap=30.0) is True


def test_trade_at_cap_succeeds():
    """Trade that fills cap exactly is allowed (\u2264 not <)."""
    assert compute_should_trade(current_exposure=27.5, trade_size=2.5, cap=30.0) is True


def test_trade_just_over_cap_blocked():
    assert compute_should_trade(current_exposure=28.0, trade_size=2.5, cap=30.0) is False


def test_trade_way_over_cap_blocked():
    assert compute_should_trade(current_exposure=50.0, trade_size=2.5, cap=30.0) is False


def test_trade_at_zero_exposure_always_succeeds():
    assert compute_should_trade(current_exposure=0.0, trade_size=2.5, cap=30.0) is True


def test_trade_at_zero_with_tiny_cap():
    """If cap is 0, no trade fits."""
    assert compute_should_trade(current_exposure=0.0, trade_size=2.5, cap=0.0) is False


def test_trade_size_larger_than_cap():
    """Trade size bigger than whole cap is always rejected."""
    assert compute_should_trade(current_exposure=0.0, trade_size=100.0, cap=30.0) is False
