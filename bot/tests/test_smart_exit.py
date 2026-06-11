"""
Tests for the smart-exit evaluation logic.
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.smart_exit import (
    PositionRecord,
    ExitDecision,
    evaluate_position,
    TAKE_PROFIT_PCT,
    CUT_LOSS_PCT,
)


def make_pos(buy_price=0.30, size=10.0, kind="directional"):
    return PositionRecord(
        token_id="0xtest",
        buy_price=buy_price,
        buy_size_shares=size,
        buy_ts="2026-06-11T00:00:00+00:00",
        kind=kind,
    )


def test_take_profit_50_on_big_up():
    pos = make_pos(buy_price=0.30)
    d = evaluate_position(pos, current_price=0.45)  # +50%
    assert d.decision == "take_profit_50"
    assert abs(d.pct_change - 0.5) < 0.01
    assert d.shares_to_sell == 5.0


def test_take_profit_50_on_huge_up():
    pos = make_pos(buy_price=0.10)
    d = evaluate_position(pos, current_price=0.50)  # +400%
    assert d.decision == "take_profit_50"
    assert d.shares_to_sell == 5.0


def test_no_profit_take_under_threshold():
    pos = make_pos(buy_price=0.30)
    d = evaluate_position(pos, current_price=0.39)  # +30%
    assert d.decision == "hold"
    assert d.shares_to_sell == 0


def test_cut_loss_100_on_big_down():
    pos = make_pos(buy_price=0.50)
    d = evaluate_position(pos, current_price=0.30)  # -40%
    assert d.decision == "cut_loss_100"
    assert abs(d.pct_change - (-0.4)) < 0.01
    assert d.shares_to_sell == 10.0


def test_cut_loss_100_at_threshold():
    pos = make_pos(buy_price=0.50)
    d = evaluate_position(pos, current_price=0.35)  # -30%
    assert d.decision == "cut_loss_100"


def test_no_loss_cut_above_threshold():
    pos = make_pos(buy_price=0.50)
    d = evaluate_position(pos, current_price=0.40)  # -20%
    assert d.decision == "hold"


def test_hold_on_small_change():
    pos = make_pos(buy_price=0.30)
    d = evaluate_position(pos, current_price=0.32)  # +7%
    assert d.decision == "hold"


def test_near_resolution_lost_triggers_sell():
    """Position that has effectively expired worthless -> sell.

    Use a buy_price of 0.20 so -30% threshold is 0.14. At current=0.02
    with 12h to resolution, position is essentially lost. Both cut_loss
    and near_res_lost would fire. Either is correct — verify the
    action is to sell 100%."""
    pos = make_pos(buy_price=0.20)
    d = evaluate_position(pos, current_price=0.02, hours_to_resolution=12)
    # cut_loss fires first (more restrictive -30%); action is correct
    assert d.shares_to_sell == 10.0
    assert d.decision in ("cut_loss_100", "near_res_lost")


def test_near_resolution_above_threshold_holds():
    """Near resolution but price is healthy (above worthless threshold
    but also above the -30% loss threshold). Use a buy_price where
    -30% wouldn't fire."""
    pos = make_pos(buy_price=0.20)
    d = evaluate_position(pos, current_price=0.18, hours_to_resolution=12)  # -10%
    assert d.decision == "hold"


def test_far_resolution_cheap_holds():
    """Far from resolution and price is low but not a realized loss
    yet (we still have time). Use a buy_price where -30% doesn't fire."""
    pos = make_pos(buy_price=0.20)
    d = evaluate_position(pos, current_price=0.18, hours_to_resolution=72)
    assert d.decision == "hold"


def test_take_profit_takes_precedence_over_loss():
    pos = make_pos(buy_price=0.20)
    d = evaluate_position(pos, current_price=0.50)
    assert d.decision == "take_profit_50"
