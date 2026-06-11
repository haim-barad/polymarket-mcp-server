"""
arb_detector.py — Multi-outcome exclusive event arbitrage detector.

Looks for events with N markets that should sum to 1.0 (the CLOB
invariant for truly-exclusive multi-outcome events). If the displayed
sum deviates from 1.0 by enough to cover fees + slippage, returns
an ArbOpportunity that the bot can execute.

The detector is a pure function over the events list (no I/O) so
it's easy to test in isolation. The actual HTTP fetch lives in the
runner; this module is just the math + a thin orderbook-depth check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ArbOpportunity:
    """A single multi-outcome arb that the bot should consider executing.

    bucket_prices[i] is the displayed Yes price for the i-th bucket.
    bucket_token_ids[i] is the CLOB token_id to buy.
    bucket_liquidity[i] is the reported group-liquidity (USD) for the
    i-th bucket. bucket_top_ask_size[i] is the size (shares) at the best
    ask; if 0 the bucket is unbuyable at the displayed price.
    """
    event_title: str
    event_id: str
    bucket_titles: tuple[str, ...]
    bucket_token_ids: tuple[str, ...]
    bucket_prices: tuple[float, ...]
    bucket_liquidity: tuple[float, ...]
    bucket_top_ask_size: tuple[float, ...]
    bucket_top_ask_price: tuple[float, ...]
    sum_yes: float
    deviation: float  # 1.0 - sum_yes (positive = arb exists)

    @property
    def gross_return_pct(self) -> float:
        """Return if you bought all buckets at the displayed prices."""
        return self.deviation / self.sum_yes if self.sum_yes > 0 else 0.0

    @property
    def n_buckets(self) -> int:
        return len(self.bucket_titles)

    @property
    def buyable_buckets(self) -> list[int]:
        """Indices of buckets that have at least 1 share at the displayed price."""
        return [i for i, s in enumerate(self.bucket_top_ask_size) if s >= 1.0]

    @property
    def max_position_size_per_bucket_usd(self) -> float:
        """The most restrictive bucket caps the size. Use the smallest
        bucket_top_ask_size to size the equal-weight position."""
        if not self.buyable_buckets:
            return 0.0
        min_size = min(self.bucket_top_ask_size[i] for i in self.buyable_buckets)
        # Convert shares to USD using the worst-fill-price (max of the
        # bucket prices, i.e., the highest displayed ask).
        worst_price = max(self.bucket_top_ask_price[i] for i in self.buyable_buckets)
        return min_size * worst_price


# Events where the displayed sum is BELOW 1.0 are the cleanest arbs:
# buy all buckets, one pays out $1.00, total cost < $1.00.
ARB_THRESHOLD = 0.03  # require sum < 0.97 (i.e. > 3% mispricing)
MIN_BUCKET_LIQUIDITY = 5000.0  # $5K per bucket (so we can fill $2.50 with room to spare)
MIN_BUYABLE_BUCKETS = 2  # need at least 2 buckets to be a multi-outcome arb


def is_exclusive_group(event: dict) -> bool:
    """An event is a candidate arb source if its markets are mutually
    exclusive (only one can resolve to Yes). Heuristics:
    - groupItemTitle is set and unique across the markets
    - endDate is the same across markets (else it's a sequential-date CDF)
    - at least 2 active markets
    """
    markets = [m for m in event.get("markets", []) if not m.get("closed")]
    if len(markets) < 2:
        return False
    titles = []
    for m in markets:
        git = m.get("groupItemTitle")
        if not git:
            return False
        if git in titles:
            return False
        titles.append(git)
    # End-date must be the same (sequential-date groups are CDFs, not arbs)
    end_dates = {m.get("endDate", "")[:10] for m in markets}
    if len(end_dates) > 1:
        return False
    # All markets should be binary (Yes/No). The "outcomes" field should
    # be '["Yes", "No"]' for each. Some markets have longer outcome lists
    # which would mean different things — skip those.
    for m in markets:
        outcomes = m.get("outcomes", "")
        if isinstance(outcomes, str):
            try:
                parsed = json.loads(outcomes)
            except Exception:
                return False
            if parsed != ["Yes", "No"]:
                return False
    return True


def compute_bucket_data(market: dict) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
    """Pull the relevant fields from a market dict for arb math.

    Returns (token_id, price, liquidity, top_ask_size). The top_ask_size
    is filled in by the runner via orderbook depth check; the detector
    doesn't make that HTTP call itself.
    """
    cid = market.get("conditionId") or market.get("condition_id")
    prices = market.get("outcomePrices", "[]")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []
    if not prices:
        return None, None, None, None
    try:
        yes_price = float(prices[0])
    except (ValueError, TypeError, IndexError):
        return None, None, None, None
    liq = market.get("liquidityNum") or market.get("liquidity") or 0
    try:
        liq = float(liq)
    except (ValueError, TypeError):
        liq = 0.0
    # Test fixtures (and the runner post-orderbook-call) can set
    # market["top_ask_size"] to inject a known depth. We honor it here.
    top_ask_size = 0.0
    try:
        top_ask_size = float(market.get("top_ask_size") or 0.0)
    except (ValueError, TypeError):
        top_ask_size = 0.0
    return None, yes_price, liq, top_ask_size


def evaluate_event(event: dict) -> Optional[ArbOpportunity]:
    """If event is a multi-outcome exclusive arb, return an ArbOpportunity.

    Returns None otherwise (insufficient buckets, sum within threshold,
    missing data, etc.).
    """
    if not is_exclusive_group(event):
        return None

    markets = [m for m in event.get("markets", []) if not m.get("closed")]
    titles, prices, liqs, token_ids, top_ask_sizes, top_ask_prices = [], [], [], [], [], []

    for m in markets:
        git = m.get("groupItemTitle")
        # Try to extract token_id (most reliable: market.tokens[0].token_id)
        token_id = None
        top_ask_price = None
        for t in m.get("tokens", []):
            if t.get("outcome") == "Yes":
                token_id = t.get("token_id")
                if token_id:
                    p = t.get("price")
                    if p is not None:
                        try:
                            top_ask_price = float(p)
                        except (ValueError, TypeError):
                            pass
                break
        # Fall back to top-level fields if tokens array is empty
        if token_id is None:
            token_id = m.get("conditionId") or m.get("id")
        _, yes_price, liq, top_ask_size = compute_bucket_data(m)
        # If we have a top_ask_price from the token, use it; else use yes_price
        # (the displayed last-trade price)
        if top_ask_price is None and yes_price is not None:
            top_ask_price = yes_price
        if yes_price is None:
            return None
        if liq < MIN_BUCKET_LIQUIDITY:
            return None
        titles.append(git)
        prices.append(yes_price)
        liqs.append(liq)
        token_ids.append(token_id or "")
        top_ask_sizes.append(top_ask_size)  # filled by runner post-fetch
        top_ask_prices.append(top_ask_price)

    sum_yes = sum(prices)
    deviation = 1.0 - sum_yes

    if deviation < ARB_THRESHOLD:
        return None  # not enough mispricing to be a real arb

    if len(titles) - sum(1 for s in top_ask_sizes if s <= 0) < MIN_BUYABLE_BUCKETS:
        # Not enough buckets have known ask data to be actionable
        return None

    return ArbOpportunity(
        event_title=event.get("title", "?")[:80],
        event_id=event.get("id", ""),
        bucket_titles=tuple(titles),
        bucket_token_ids=tuple(token_ids),
        bucket_prices=tuple(prices),
        bucket_liquidity=tuple(liqs),
        bucket_top_ask_size=tuple(top_ask_sizes),
        bucket_top_ask_price=tuple(top_ask_prices),
        sum_yes=sum_yes,
        deviation=deviation,
    )


def scan_events(events: list[dict]) -> list[ArbOpportunity]:
    """Scan a list of events (as returned by the gamma API) for arbs.
    Returns opportunities sorted by gross_return_pct descending.
    """
    out = []
    for e in events:
        opp = evaluate_event(e)
        if opp is not None:
            out.append(opp)
    out.sort(key=lambda o: o.gross_return_pct, reverse=True)
    return out
