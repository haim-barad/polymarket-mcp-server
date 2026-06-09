"""
onchain_reconcile.py — Pull the on-chain truth from data-api.polymarket.com
and update the local state. This is the bot's only window into the user's
real wallet; without it the bot is blind to positions held before it started
running, or to positions placed by the smoke test runs that left filled
orders on-chain even after the cancel-all on shutdown.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.request import Request, urlopen

from bot.config import BotConfig
from bot.state_manager import StateManager


DATA_API = "https://data-api.polymarket.com/positions"


def fetch_positions(wallet_address: str, *, min_value: float = 0.01, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Fetch the wallet's open positions from data-api.

    Returns a list of normalized dicts:
        [{title, condition_id, outcome, size, current_value, initial_value, ...}]
    Empty list on any error.
    """
    if not wallet_address:
        return []
    url = f"{DATA_API}?user={wallet_address}&sizeThreshold={min_value}"
    try:
        req = Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for p in data:
        try:
            out.append({
                "title": p.get("title", ""),
                "condition_id": p.get("conditionId") or p.get("condition_id") or "",
                "outcome": p.get("outcome", ""),
                "size": float(p.get("size", 0) or 0),
                "current_value": float(p.get("currentValue", 0) or 0),
                "initial_value": float(p.get("initialValue", 0) or 0),
                "cash_pnl": float(p.get("cashPnl", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def reconcile(
    state_manager: StateManager,
    wallet_address: str,
    config: Optional[BotConfig] = None,
) -> dict[str, Any]:
    """Pull on-chain truth and update the local state.

    Returns a summary dict:
        {
            "open_position_count": int,
            "open_exposure_usd": float,
            "positions_by_market": {condition_id: {outcome: size}},
            "over_exposure_limit": bool,
            "exceeded_by_usd": float,
        }
    """
    cfg = config or BotConfig.load()
    positions = fetch_positions(wallet_address, min_value=0.01)

    total_exposure = sum(p["current_value"] for p in positions)
    positions_by_market: dict[str, dict[str, float]] = {}
    for p in positions:
        cid = p["condition_id"]
        outcome = p["outcome"]
        if cid not in positions_by_market:
            positions_by_market[cid] = {}
        positions_by_market[cid][outcome] = (
            positions_by_market[cid].get(outcome, 0.0) + p["size"]
        )

    over_limit = total_exposure > cfg.total_open_exposure_usd
    exceeded_by = max(0.0, total_exposure - cfg.total_open_exposure_usd)

    # Persist to state
    state_manager.update(
        open_position_count=len(positions),
        open_exposure_usd=round(total_exposure, 2),
    )

    return {
        "open_position_count": len(positions),
        "open_exposure_usd": round(total_exposure, 2),
        "positions_by_market": positions_by_market,
        "over_exposure_limit": over_limit,
        "exceeded_by_usd": round(exceeded_by, 2),
    }
