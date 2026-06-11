"""
Tests for the multi-outcome arb detector.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.arb_detector import (
    ARB_THRESHOLD,
    is_exclusive_group,
    evaluate_event,
    scan_events,
    ArbOpportunity,
)


def make_event(title, group_buckets, end_date="2026-11-30", liq=10000, top_ask_size=100.0):
    """Helper: build a fake event with N markets, each with a groupItemTitle,
    endDate, and a Yes price. The top_ask_size is the depth at the
    displayed price (default 100 shares, plenty for our $2.50 test trades)."""
    markets = []
    for i, item in enumerate(group_buckets):
        if len(item) == 2:
            bucket_title, yes_price = item
            ask_price = yes_price
            ask_size = top_ask_size
        else:
            bucket_title, yes_price, ask_size = item
            ask_price = yes_price
        cid = f"0x{i:064x}"
        markets.append({
            "id": str(1000 + i),
            "conditionId": cid,
            "question": f"{title} ({bucket_title})",
            "groupItemTitle": bucket_title,
            "endDate": end_date,
            "liquidityNum": liq,
            "liquidity": liq,
            "outcomePrices": json.dumps([str(yes_price), str(round(1 - yes_price, 4))]),
            "outcomes": json.dumps(["Yes", "No"]),
            "closed": False,
            "tokens": [{"token_id": f"0xtok{i:064x}", "outcome": "Yes", "price": ask_price}],
            # Runner injects this after orderbook call. Test fixtures
            # pre-populate it under the same key the detector reads.
            "top_ask_size": ask_size,
        })
    return {
        "id": "event-1",
        "title": title,
        "endDate": end_date,
        "closed": False,
        "markets": markets,
    }


# === is_exclusive_group tests ===

def test_exclusive_group_with_unique_buckets_passes():
    e = make_event("Test", [("A", 0.3), ("B", 0.4), ("C", 0.3)])
    assert is_exclusive_group(e) is True


def test_single_market_fails():
    e = make_event("Test", [("A", 0.5)])
    assert is_exclusive_group(e) is False


def test_duplicate_groupitem_titles_fails():
    e = make_event("Test", [("A", 0.3), ("A", 0.4), ("A", 0.3)])
    assert is_exclusive_group(e) is False


def test_different_end_dates_fails():
    e = make_event("Test", [
        ("A", 0.3),
        ("B", 0.4),
    ])
    # Manually set one market's end date differently
    e["markets"][1]["endDate"] = "2026-12-31"
    assert is_exclusive_group(e) is False


def test_multi_outcome_market_fails():
    """A market with >2 outcomes (e.g. multi-candidate) is suspicious."""
    e = make_event("Test", [("A", 0.3), ("B", 0.4)])
    e["markets"][0]["outcomes"] = json.dumps(["Yes", "No", "Maybe"])
    assert is_exclusive_group(e) is False


# === evaluate_event tests ===

def test_clear_underpriced_arb_returns_opportunity():
    e = make_event("Who will X endorse?", [
        ("A", 0.20), ("B", 0.20), ("C", 0.20), ("D", 0.20),
    ])  # sum = 0.80, dev = 0.20
    opp = evaluate_event(e)
    assert opp is not None
    assert opp.sum_yes == 0.80
    assert abs(opp.deviation - 0.20) < 0.001
    assert opp.gross_return_pct > 0.24  # 0.20 / 0.80 = 25%


def test_sum_at_threshold_returns_none():
    """If sum is within ARB_THRESHOLD of 1.0, no arb."""
    # ARB_THRESHOLD is 0.03 so sum must be < 0.97. sum=0.98 should NOT arb.
    e = make_event("Test", [("A", 0.49), ("B", 0.49)])
    opp = evaluate_event(e)
    assert opp is None


def test_just_under_threshold_returns_opportunity():
    e = make_event("Test", [("A", 0.48), ("B", 0.48)])
    # sum=0.96, dev=0.04, exceeds 0.03 threshold
    opp = evaluate_event(e)
    assert opp is not None


def test_overpriced_group_returns_none():
    """sum > 1.0 in exclusive event = over-priced, but no arb opportunity
    without info edge. Detector skips it (would need to know which
    candidate to short)."""
    e = make_event("Test", [("A", 0.50), ("B", 0.60)])  # sum=1.10
    opp = evaluate_event(e)
    assert opp is None  # we don't auto-short the over-priced ones


def test_insufficient_liquidity_fails():
    """If a bucket has < MIN_BUCKET_LIQUIDITY, reject the whole event."""
    e = make_event("Test", [("A", 0.40), ("B", 0.40)], liq=1000)  # only $1K per bucket
    opp = evaluate_event(e)
    assert opp is None


def test_missing_yes_price_returns_none():
    e = make_event("Test", [("A", 0.5), ("B", 0.5)])
    e["markets"][0]["outcomePrices"] = "[]"
    opp = evaluate_event(e)
    assert opp is None


def test_sequential_date_event_returns_none():
    """By Jun 30 / by Dec 31 is a CDF, not exclusive. Detector skips."""
    e = make_event("Test", [("A", 0.10), ("B", 0.50)], end_date="2026-12-31")
    e["markets"][0]["endDate"] = "2026-06-30"  # first bucket is "by Jun 30"
    opp = evaluate_event(e)
    assert opp is None  # different end dates -> not exclusive


# === scan_events tests ===

def test_scan_returns_only_arbs_sorted_by_return():
    e1 = make_event("Weinstein-style", [("A", 0.30), ("B", 0.30)])  # sum=0.60, dev=0.40 (40%)
    e2 = make_event("TikTok-style", [("A", 0.10), ("B", 0.10), ("C", 0.10)])  # sum=0.30 (best arb)
    e3 = make_event("Bernie-style", [("A", 0.20), ("B", 0.20)])  # sum=0.40 (decent arb)
    e4 = make_event("Fair", [("A", 0.50), ("B", 0.50)])  # sum=1.0 (no arb)

    opps = scan_events([e1, e2, e3, e4])
    # Should return 3 arbs, sorted by gross_return_pct desc.
    # TikTok  (sum=0.30, dev=0.70, return=233%)
    # Bernie  (sum=0.40, dev=0.60, return=150%)
    # Weinstein(sum=0.60, dev=0.40, return=67%)
    assert len(opps) == 3
    assert opps[0].event_title.startswith("TikTok")
    assert opps[1].event_title.startswith("Bernie")
    assert opps[2].event_title.startswith("Weinstein")
    # Also verify the math: TikTok 233% > Bernie 150% > Weinstein 67%
    assert opps[0].gross_return_pct > opps[1].gross_return_pct > opps[2].gross_return_pct
