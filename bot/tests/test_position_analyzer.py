"""
Tests for the position analyzer.
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.position_analyzer import (
    Position, CostBasis, analyze_position, render_markdown_report,
)


def make_pos(buy_price=0.30, size=10.0, outcome="Yes", title="Test?", price=0.30,
             asset="0xasset", condition_id="0xcid"):
    return Position(
        condition_id=condition_id,
        asset=asset,
        title=title,
        outcome=outcome,
        size=size,
        current_value_usd=size * price,
        current_price=price,
        avg_price=buy_price,
        end_date=None,
    )


def make_cost(buy_price=0.30, size=10.0, kind="directional"):
    return CostBasis(
        buy_price=buy_price,
        buy_size_shares=size,
        kind=kind,
        last_evaluated_ts=None,
        last_decision=None,
    )


def test_yes_position_holds_pays_1_per_share():
    pos = make_pos(buy_price=0.30, size=10.0, outcome="Yes", price=0.40)
    cost = make_cost(buy_price=0.30, size=10.0)
    market = {}  # no endDate
    a = analyze_position(pos, cost, market)
    # If YES wins, payout = 10 shares × $1 = $10. Cost = $3. P&L = $7.
    assert a["pnl_if_yes_wins"] == 10.0 - 3.0
    # If NO wins, payout = 0. Cost = $3. P&L = -$3.
    assert a["pnl_if_no_wins"] == -3.0
    # Mark-to-market: 10 × 0.40 - 3 = 1
    assert abs(a["pnl_mark_to_market"] - 1.0) < 0.01


def test_no_position_pays_if_no_wins():
    pos = make_pos(buy_price=0.30, size=10.0, outcome="No", price=0.50)
    cost = make_cost(buy_price=0.30, size=10.0)
    a = analyze_position(pos, cost, {})
    # If NO wins, we get $1/share = $10, minus cost $3 = $7
    assert a["pnl_if_no_wins"] == 10.0 - 3.0
    # If YES wins, we get 0, cost was $3
    assert a["pnl_if_yes_wins"] == -3.0


def test_ev_at_implied_prob_uses_current_price():
    """If position is at implied prob p_yes, the EV = p_yes * (win) + p_no * (lose)."""
    pos = make_pos(buy_price=0.30, size=10.0, outcome="Yes", price=0.50)
    cost = make_cost(buy_price=0.30, size=10.0)
    a = analyze_position(pos, cost, {})
    # p_yes = 0.50 (displayed price)
    # win = $7, lose = -$3
    # EV per share = 0.5 * 0.7 + 0.5 * (-0.3) = 0.20
    # EV total = 0.20 * 10 = $2.00
    assert abs(a["ev_at_implied_prob"] - 2.0) < 0.01


def test_ev_sensitivity_shifts_p_yes():
    pos = make_pos(buy_price=0.30, size=10.0, outcome="Yes", price=0.50)
    cost = make_cost(buy_price=0.30, size=10.0)
    a = analyze_position(pos, cost, {})
    # +20% shift: p_yes = 0.70
    # win = $7, lose = -$3
    # EV per share = 0.7 * 0.7 + 0.3 * (-0.3) = 0.49 - 0.09 = 0.40
    # EV total = 0.40 * 10 = $4.00
    ev_plus20 = a["ev_sensitivity"]["prob_+20pct"]
    assert abs(ev_plus20 - 4.0) < 0.01, f"expected 4.0, got {ev_plus20}"
    # -20% shift: p_yes = 0.30
    # EV per share = 0.3 * 0.7 + 0.7 * (-0.3) = 0.21 - 0.21 = 0.0
    # EV total = 0
    ev_minus20 = a["ev_sensitivity"]["prob_-20pct"]
    assert abs(ev_minus20 - 0.0) < 0.01, f"expected 0, got {ev_minus20}"


def test_arb_bucket_note_added():
    pos = make_pos(buy_price=0.10, size=100.0, price=0.20)
    cost = make_cost(buy_price=0.10, size=100.0, kind="arb_bucket")
    a = analyze_position(pos, cost, {})
    assert any("arb" in n.lower() for n in a["notes"])


def test_mark_to_market_loss_flag():
    pos = make_pos(buy_price=0.50, size=10.0, price=0.10)  # -80% loss
    cost = make_cost(buy_price=0.50, size=10.0)
    a = analyze_position(pos, cost, {})
    assert any("mark-to-market loss" in n for n in a["notes"])


def test_days_to_resolution_calculated():
    pos = make_pos(buy_price=0.30, size=10.0, price=0.40)
    cost = make_cost(buy_price=0.30, size=10.0)
    future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    a = analyze_position(pos, cost, {"endDate": future})
    assert a["days_to_resolution"] is not None
    assert 9 <= a["days_to_resolution"] <= 11


def test_render_markdown_report_includes_summary():
    pos = make_pos(buy_price=0.30, size=10.0, price=0.40, title="Test Market")
    cost = make_cost(buy_price=0.30, size=10.0)
    a = analyze_position(pos, cost, {})
    report = render_markdown_report([a], datetime.now(timezone.utc))
    assert "Test Market" in report
    assert "## Summary" in report
    assert "## Per-Position Analysis" in report
    assert "Cost basis" in report
    assert "Mark-to-market P&L" in report


def test_position_from_data_api_normalizes():
    """Test the data-api parsing normalizes numbers correctly."""
    raw = {
        "conditionId": "0xabc",
        "asset": "0xdef",
        "title": "Test",
        "outcome": "Yes",
        "size": 10,
        "currentValue": 3.5,
        "avgPrice": 0.4,
        "endDate": "2026-12-31",
    }
    p = Position.from_data_api(raw)
    assert p.condition_id == "0xabc"
    assert p.asset == "0xdef"
    assert p.size == 10.0
    assert p.current_value_usd == 3.5
    assert abs(p.current_price - 0.35) < 0.001
    assert p.avg_price == 0.4
