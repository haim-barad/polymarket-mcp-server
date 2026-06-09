"""Signal — option (a) in-band mechanical."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from bot.config import BotConfig


@dataclass(frozen=True)
class SignalDecision:
    accepted: bool
    reason: str
    size_usd: float = 0.0


# Off-topic content filter (canonical location). The MCP gamma API\'s "Sports"
# tag is loose — it includes pop culture, news, and "before-event-X" markets.
# Reject anything that doesn\'t look like a real sports / high-tech / news
# question. Word-boundary check so "GTA" doesn\'t trip on "GTA VI".
import re as _re
_OFFTOPIC_PATTERNS = (
    r"\balbum\b", r"\bmovie\b", r"\btv show\b", r"\btv series\b",
    r"\bsong\b", r"\bsingle\b", r"\brelease party\b", r"\bgrammy\b",
    r"\boscar\b", r"\bemmy\b", r"\bcelebrity\b", r"\bkardashian\b",
    r"\btwitter\b", r"\btiktok\b", r"\binstagram\b", r"\byoutube\b",
    r"\bbox office\b", r"\bstreaming\b", r"\bspotify\b", r"\bbillboard\b",
    r"\bnft\b", r"\bcrypto price\b", r"\bbitcoin price\b", r"\beth price\b",
)
_OFFTOPIC_RE = _re.compile("|".join(_OFFTOPIC_PATTERNS), _re.IGNORECASE)


def _is_offtopic(question: str) -> Optional[str]:
    """Return the off-topic keyword matched, or None if clean."""
    m = _OFFTOPIC_RE.search(question)
    return m.group(0) if m else None


def evaluate_market(
    market: dict, now_utc: Optional[str] = None,
    config: Optional[BotConfig] = None,
) -> SignalDecision:
    cfg = config or BotConfig.load()
    now = (datetime.fromisoformat(now_utc.replace("Z", "+00:00"))
           if now_utc else datetime.now(timezone.utc))

    category = (market.get("category") or "").lower()
    if category not in [c.lower() for c in cfg.enabled_categories]:
        return SignalDecision(False, f"category \'{category}\' not in enabled set")

    question = (market.get("question") or market.get("slug") or "")
    question_lc = question.lower()

    # 1. Off-topic content filter (canonical location)
    off = _is_offtopic(question)
    if off:
        return SignalDecision(False, f"off-topic content: \'{off}\'")

    # 2. Per-category keyword blacklist
    blacklist = list(cfg.sports_blacklist_keywords)
    if category in ("hightech", "high_tech", "high-tech"):
        blacklist += list(cfg.hightech_blacklist_keywords)
    for kw in blacklist:
        if kw.lower() in question_lc:
            return SignalDecision(False, f"blacklist keyword \'{kw}\' in \'{question[:60]}\'")

    # 3. Sports whitelist: question must mention at least one whitelisted league.
    #    Polymarket\'s gamma "Sports" tag is loose; this narrows to real sports.
    if category == "sports" and cfg.sports_whitelist:
        if not any(league.lower() in question_lc for league in cfg.sports_whitelist):
            leagues = ", ".join(cfg.sports_whitelist[:4])
            return SignalDecision(False, f"no whitelisted league in question (want one of: {leagues}…)")

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
