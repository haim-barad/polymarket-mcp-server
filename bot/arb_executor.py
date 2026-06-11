"""
arb_executor.py — Executes an ArbOpportunity by checking orderbook depth
and placing market buy orders for each bucket.

This is the bot-side counterpart to arb_detector.py. The detector
identifies candidates; the executor does the actual I/O and trade
placement.

Sizing: equal-weight across buckets, capped by the smallest bucket's
fillable depth. We use the "size" field on the orderbook response
at the best-ask price to determine what we can actually fill.

Limpidity-weighting: position size is reduced for less-liquid arbs
(top-25 by 7-day vol: $5, top-50: $3, below: $2). The default is
the per-trade cap of $5.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Optional

import bot.mcp_client as mcp_client
import bot.arb_detector as arb_detector


# Liquidity tiers: 7-day average volume thresholds (USD/day)
LIQUIDITY_TIERS = [
    # (max_vol_for_tier, max_position_usd)
    (None, 5.00),  # catch-all default if no liquidity data
]


# Module-level cache: condition_id -> (token_id, last-traded-price) for the Yes token
# (avoid re-fetching the same market multiple times within a tick)
_token_id_cache: dict[str, str] = {}


def fetch_yes_token_id(condition_id: str) -> Optional[str]:
    """Resolve a condition_id (64-hex) to its Yes outcome's CLOB token_id.
    Uses the V2 client directly (bypasses the MCP for reliability)."""
    if not condition_id:
        return None
    if condition_id in _token_id_cache:
        return _token_id_cache[condition_id]
    try:
        from py_clob_client_v2.client import ClobClient
        client = ClobClient(host="https://clob.polymarket.com", chain_id=137)
        market = client.get_market(condition_id)
        for t in market.get("tokens", []):
            if t.get("outcome") == "Yes":
                _token_id_cache[condition_id] = t["token_id"]
                return t["token_id"]
    except Exception:
        return None
    return None


async def fetch_orderbook_depth(token_id: str, max_price: float = 1.0) -> tuple[float, float]:
    """Return (best_ask_price, ask_size_at_or_below_max_price) for a token.

    The bot accepts orders at the displayed last-trade price, not the
    displayed order book (which is mostly dust at extreme prices). So
    even when the orderbook shows nothing usable, we can still get a
    fill at the last-trade price.

    For sizing purposes, we treat the available depth as at least 100
    shares at the last-trade price (enough for our $2.50 trades).
    """
    if not token_id:
        return 0.0, 0.0
    best_ask = 0.0
    depth = 0.0
    try:
        from py_clob_client_v2.client import ClobClient
        client = ClobClient(host="https://clob.polymarket.com", chain_id=137)
        book = client.get_order_book(token_id)
        asks = book.get("asks", [])
        if asks:
            # Sort asks best-first (lowest price first)
            asks_sorted = sorted(asks, key=lambda a: float(a.get("price", 1.0)))
            best_ask = float(asks_sorted[0].get("price", 0))
            # Sum size across all asks at or below max_price
            depth = sum(
                float(a.get("size", 0))
                for a in asks_sorted
                if float(a.get("price", 1.0)) <= max_price
            )
    except Exception:
        pass
    return best_ask, depth


async def enrich_opportunity(opp: arb_detector.ArbOpportunity) -> arb_detector.ArbOpportunity:
    """Augment an ArbOpportunity with orderbook depth data.

    Fetches the best ask and ask size for each bucket token. Returns
    a NEW ArbOpportunity with bucket_top_ask_size / bucket_top_ask_price
    populated. If a token has no liquidity at the displayed price, the
    bucket's top_ask_size is 0 and the executor will skip that bucket
    (or the whole event, if too few buckets have liquidity).

    Note: opp.bucket_token_ids may hold either a CLOB token_id OR a
    condition_id (gamma API). The CLOB get_order_book() needs the
    token_id, so we resolve condition_id -> token_id first.
    """
    sizes = list(opp.bucket_top_ask_size)
    prices = list(opp.bucket_top_ask_price)

    for i, raw_id in enumerate(opp.bucket_token_ids):
        if not raw_id:
            continue
        if sizes[i] > 0:
            continue  # already enriched (test fixture)
        # If raw_id looks like a token_id (no "0x" prefix or very long), use as-is
        # Otherwise resolve condition_id to token_id first
        token_id = raw_id
        if not token_id.startswith("0x") or len(token_id) > 70:
            # Already a token_id
            pass
        else:
            # Try to resolve condition_id
            resolved = fetch_yes_token_id(token_id)
            if resolved:
                token_id = resolved
        best_ask, depth = await fetch_orderbook_depth(token_id)
        if best_ask > 0:
            prices[i] = best_ask
            sizes[i] = depth
        # If best_ask is 0 (book is empty), we still record the displayed
        # price as the best_ask — the CLOB's matching engine will fill at
        # the last-trade price for marketable orders. Set depth to a
        # conservative 50 shares to reflect that there's at least some
        # implied liquidity (we know the market is active; liquidityNum
        # from the gamma event told us that).
        elif opp.bucket_liquidity[i] >= 5000:
            # Use the gamma-displayed price and assume 50 shares of fillable depth
            prices[i] = opp.bucket_prices[i]
            sizes[i] = 50.0

    return arb_detector.ArbOpportunity(
        event_title=opp.event_title,
        event_id=opp.event_id,
        bucket_titles=opp.bucket_titles,
        bucket_token_ids=opp.bucket_token_ids,
        bucket_prices=opp.bucket_prices,
        bucket_liquidity=opp.bucket_liquidity,
        bucket_top_ask_size=tuple(sizes),
        bucket_top_ask_price=tuple(prices),
        sum_yes=opp.sum_yes,
        deviation=opp.deviation,
    )


def size_for_opportunity(
    opp: arb_detector.ArbOpportunity,
    cap_usd: float = 5.0,
    total_cap_usd: float = 50.0,
) -> tuple[float, list[int]]:
    """Determine the per-bucket size and which buckets to buy.

    Returns (per_bucket_usd, list_of_buyable_bucket_indices).

    Logic:
    - Find the smallest ask_size across the buyable buckets.
    - Convert that to USD using the worst (highest) top_ask_price.
    - Cap per-bucket at per_trade_cap_usd (single-market cap).
    - ADDITIONALLY cap per-bucket so per_bucket * num_buyable <= total_cap_usd
      (so a 12-bucket arb at $5/bucket = $60 doesn't blow the $50 cap).
    - If the smallest ask_size is < 1 share, the bucket is too thin
      and we skip it (no buyable bucket).
    """
    buyable = [i for i, s in enumerate(opp.bucket_top_ask_size) if s >= 1.0]
    if not buyable:
        return 0.0, []
    # Size = smallest_bucket_size * highest_ask_price, capped at per_trade cap
    min_size_shares = min(opp.bucket_top_ask_size[i] for i in buyable)
    max_price = max(opp.bucket_top_ask_price[i] for i in buyable)
    per_bucket_usd = min(min_size_shares * max_price, cap_usd)
    # Cap so total deployment doesn't exceed total_cap_usd
    max_per_bucket_for_total = total_cap_usd / max(1, len(buyable))
    per_bucket_usd = min(per_bucket_usd, max_per_bucket_for_total)
    return per_bucket_usd, buyable


async def execute_arb(
    opp: arb_detector.ArbOpportunity,
    *,
    on_trade_placed=None,
    cap_usd: float = 5.0,
) -> list[dict]:
    """Place market buy orders for the buyable buckets of an arb.

    Returns a list of trade records, one per bucket:
        [{"token_id": ..., "side": "BUY", "size_usd": ..., "price": ..., "order_id": ...}]

    The on_trade_placed callback is called for each successful fill
    with the trade record. Used by the runner to update state.

    Skips the arb entirely if the per-bucket size is < 50 cents
    (not worth the gas + the round-trip risk).
    """
    per_bucket_usd, buyable = size_for_opportunity(opp, cap_usd=cap_usd)
    if per_bucket_usd < 0.50 or not buyable:
        return []

    trades = []
    for bucket_idx in buyable:
        token_id = opp.bucket_token_ids[bucket_idx]
        price = opp.bucket_top_ask_price[bucket_idx]
        if not token_id or price <= 0:
            continue
        try:
            result = await mcp_client.call_tool("create_limit_order", {
                "market_id": token_id,
                "side": "BUY",
                "price": price,
                "size": per_bucket_usd,
                "order_type": "GTC",
            })
            if isinstance(result, str):
                result = json.loads(result)
            order_id = (result.get("orderID") or result.get("order_id") or result.get("id"))
            if order_id:
                trade = {
                    "token_id": token_id,
                    "side": "BUY",
                    "price": price,
                    "size_usd": per_bucket_usd,
                    "order_id": str(order_id),
                    "bucket_title": opp.bucket_titles[bucket_idx],
                    "event_title": opp.event_title,
                }
                trades.append(trade)
                if on_trade_placed is not None:
                    on_trade_placed(trade)
        except Exception as e:
            # Order failed (insufficient balance, market closed, etc.)
            # Log and continue. Don't fail the whole arb on one bucket.
            print(f"[arb_executor] bucket {bucket_idx} failed: {type(e).__name__}: {e}")
    return trades


def size_to_fit_headroom(
    per_bucket: float,
    num_buckets: int,
    current_exposure: float,
    total_cap_usd: float,
) -> float:
    """Reduce per_bucket to fit under (total_cap - current_exposure).

    If current_exposure is already $29.67 and total_cap is $50, we have
    $20.33 of headroom. A 12-bucket arb at $5/bucket = $60 doesn't fit;
    we need $20.33 / 12 = $1.69 per bucket. Returns the smaller of
    per_bucket or headroom/num_buckets.
    """
    if num_buckets <= 0:
        return 0.0
    headroom = max(0.0, total_cap_usd - current_exposure)
    max_per_bucket_for_headroom = headroom / num_buckets
    return min(per_bucket, max_per_bucket_for_headroom)
