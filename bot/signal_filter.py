"""Signal — option (a) in-band mechanical."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import BotConfig


@dataclass(frozen=True)
class SignalDecision:
    accepted: bool
    reason: str
    size_usd: float = 0.0


def evaluate_market(
    market: dict, now_utc: Optional[str] = None,
    config: Optional[BotConfig] = None,
) -> SignalDecision:
    cfg = config or BotConfig.load()
    now = (datetime.fromisoformat(now_utc.replace("Z", "+00:00"))
           if now_utc else datetime.now(timezone.utc))

    category = (market.get("category") or "").lower()
    if category not in [c.lower() for c in cfg.enabled_categories]:
        return SignalDecision(False, f"category '{category}' not in enabled set")

    question = (market.get("question") or market.get("slug") or "").lower()

    blacklist = list(cfg.sports_blacklist_keywords)
    if category in ("hightech", "high_tech", "high-tech"):
        blacklist += list(cfg.hightech_blacklist_keywords)
    for kw in blacklist:
        if kw.lower() in question:
            return SignalDecision(False, f"blacklist keyword '{kw}' in '{question[:60]}'")

    best_ask = market.get("best_ask")
    if best_ask is None or not (cfg.in_band_low <= best_ask <= cfg.in_band_high):
        return SignalDecision(False, f"best_ask {best_ask} outside band [{cfg.in_band_low}, {cfg.in_band_high}]")

    liq = market.get("liquidity_usd", 0.0) or 0.0
    if liq < cfg.min_liquidity_usd:
        return SignalDecision(False, f"liquidity ${liq:.0f} below ${cfg.min_liquidity_usd:.0f}")

    end_str = market.get("end_date_utc")
    if not end_str:
        return SignalDecision(False, "no end_date_utc")
    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    ttr = end - now
    if ttr < timedelta(hours=cfg.time_to_resolution_min_hours):
        return SignalDecision(False, f"time-to-resolution {ttr} below 6h floor")
    if ttr > timedelta(days=cfg.time_to_resolution_max_days):
        return SignalDecision(False, f"time-to-resolution {ttr} above 7d ceiling")

    return SignalDecision(True, "in-band + liquid + on-time", size_usd=cfg.per_trade_usd)
