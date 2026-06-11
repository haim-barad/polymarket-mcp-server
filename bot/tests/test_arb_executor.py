"""
Tests for the arb executor sizing logic.

The actual order placement is exercised end-to-end via the live bot.
These tests focus on the pure-function sizing logic which is the
deterministic core of the executor.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.arb_detector import ArbOpportunity
from bot.arb_executor import size_for_opportunity, LIQUIDITY_TIERS


def make_opp(*, prices=(0.30, 0.30), sizes=(100, 100), asks=(0.30, 0.30)):
    return ArbOpportunity(
        event_title="Test",
        event_id="e1",
        bucket_titles=("A", "B"),
        bucket_token_ids=("0xaaa", "0xbbb"),
        bucket_prices=prices,
        bucket_liquidity=(10000.0, 10000.0),
        bucket_top_ask_size=sizes,
        bucket_top_ask_price=asks,
        sum_yes=sum(prices),
        deviation=1.0 - sum(prices),
    )


def test_size_capped_by_smallest_bucket():
    """If one bucket has 50 shares and the other has 200, size is 50
    shares * the worst price (rounded up)."""
    opp = make_opp(sizes=(50, 200), asks=(0.30, 0.30))
    per_bucket, buyable = size_for_opportunity(opp, cap_usd=5.0)
    assert buyable == [0, 1]
    # min size 50 shares * max price 0.30 = $15, capped to $5
    assert per_bucket == 5.0


def test_size_capped_by_cap():
    """If orderbook depth allows > $5, we cap at the trade cap."""
    opp = make_opp(sizes=(1000, 1000), asks=(0.30, 0.30))
    per_bucket, buyable = size_for_opportunity(opp, cap_usd=5.0)
    assert buyable == [0, 1]
    assert per_bucket == 5.0


def test_size_below_floor_skips():
    """If per-bucket size is < $0.50, the executor skips the arb."""
    opp = make_opp(sizes=(1, 1), asks=(0.30, 0.30))  # 1 * 0.30 = $0.30
    per_bucket, buyable = size_for_opportunity(opp, cap_usd=5.0)
    # 1 * 0.30 = $0.30, below the 0.50 floor
    assert per_bucket == 0.30
    assert buyable == [0, 1]
    # Caller (execute_arb) checks for < 0.50 and skips


def test_no_buyable_buckets_returns_zero():
    """If all buckets have 0 size, no buyable buckets."""
    opp = make_opp(sizes=(0, 0))
    per_bucket, buyable = size_for_opportunity(opp, cap_usd=5.0)
    assert buyable == []
    assert per_bucket == 0.0


def test_one_bucket_thin_other_deep():
    """If one bucket has 1 share (too thin), it still counts as buyable
    but caps the size."""
    opp = make_opp(sizes=(1, 1000), asks=(0.30, 0.30))
    per_bucket, buyable = size_for_opportunity(opp, cap_usd=5.0)
    # min size 1 share, max price 0.30 = $0.30, below the 0.50 floor
    # so caller should skip
    assert buyable == [0, 1]
    assert per_bucket == 0.30
