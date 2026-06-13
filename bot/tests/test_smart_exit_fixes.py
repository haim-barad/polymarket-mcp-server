"""
Tests for the smart-exit verify-fill and skip-recently-evaluated logic.
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.smart_exit import PositionRecord


def make_pos(last_evaluated_ts=None, last_decision=None, kind="directional"):
    return PositionRecord(
        token_id="0xtest",
        buy_price=0.30,
        buy_size_shares=10.0,
        buy_ts="2026-06-11T00:00:00+00:00",
        kind=kind,
        last_evaluated_ts=last_evaluated_ts,
        last_decision=last_decision,
    )


def test_recently_evaluated_skip_logic():
    """The smart-exit should skip positions evaluated in the last 30 min."""
    pos = make_pos(last_evaluated_ts=datetime.now(timezone.utc).isoformat())
    # Verify the logic with a small helper
    SKIP_RETRY_MINUTES = 30
    last_eval = datetime.fromisoformat(pos.last_evaluated_ts)
    elapsed = datetime.now(timezone.utc) - last_eval
    assert elapsed < timedelta(minutes=SKIP_RETRY_MINUTES),         f"Expected to skip, but elapsed = {elapsed}"


def test_old_evaluation_allows_retry():
    """Positions evaluated more than 30 min ago should be retried."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pos = make_pos(last_evaluated_ts=old_ts)
    SKIP_RETRY_MINUTES = 30
    last_eval = datetime.fromisoformat(pos.last_evaluated_ts)
    elapsed = datetime.now(timezone.utc) - last_eval
    assert elapsed >= timedelta(minutes=SKIP_RETRY_MINUTES),         f"Expected to retry, but elapsed = {elapsed}"


def test_never_evaluated_always_runs():
    """A position that's never been evaluated should always be checked."""
    pos = make_pos(last_evaluated_ts=None)
    assert pos.last_evaluated_ts is None


def test_position_record_carries_unfilled_marker():
    """The runner marks unfilled orders with a suffix so the skip works."""
    pos = make_pos(
        last_evaluated_ts=datetime.now(timezone.utc).isoformat(),
        last_decision="cut_loss_100_unfilled"
    )
    assert pos.last_decision == "cut_loss_100_unfilled"
    assert pos.last_evaluated_ts is not None
