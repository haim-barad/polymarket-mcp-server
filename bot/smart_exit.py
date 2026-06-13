"""
smart_exit.py — Active management of open positions.

For directional positions (i.e. NOT arb buckets), decide whether to
take profit, cut loss, or hold. Runs on every tick.

Exit rules (per direction position):
  * current_price up ≥ +50% from buy:  SELL 50%  (take profit)
  * current_price down ≤ -30% from buy: SELL 100% (cut loss)
  * hours-to-resolution < 24h AND price < 5¢: SELL 100% (effectively lost)
  * on-chain exposure > 90% of cap:    consider exits to free room
  * otherwise:                          hold

Arb buckets are NEVER smart-exited. They must be held to resolution
for the arb to pay out.

Position cost-basis is tracked in the SQLite `positions_cost` table
(written by the executor on each fill, read by this module).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import bot.mcp_client as mcp_client


# Thresholds (overridable via env or config in the future)
TAKE_PROFIT_PCT = 0.50  # sell 50% if position is up 50%+
CUT_LOSS_PCT = -0.30     # sell 100% if position is down 30%+
NEAR_RESOLUTION_HOURS = 24  # if < 24h to resolution and price < 5¢, sell
WORTHLESS_PRICE = 0.05
EXPOSURE_CRITICAL = 0.90  # if exposure > 90% of cap, consider exits


@dataclass
class PositionRecord:
    """A row from positions_cost."""
    token_id: str
    buy_price: float
    buy_size_shares: float
    buy_ts: str
    kind: str  # "directional" or "arb_bucket"
    last_evaluated_ts: Optional[str] = None
    last_decision: Optional[str] = None


@dataclass
class ExitDecision:
    """Result of evaluating one position for exit."""
    token_id: str
    decision: str  # "hold", "take_profit_50", "cut_loss_100", "near_res_lost"
    current_price: float
    buy_price: float
    pct_change: float
    shares_to_sell: float
    reasoning: str


def _connect(state_dir):
    db = state_dir / "bot.db"
    con = sqlite3.connect(str(db), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def ensure_positions_cost_table(state_dir) -> None:
    """Create the positions_cost table if it doesn't exist."""
    con = _connect(state_dir)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS positions_cost (
                token_id TEXT PRIMARY KEY,
                buy_price REAL NOT NULL,
                buy_size_shares REAL NOT NULL,
                buy_ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                last_evaluated_ts TEXT,
                last_decision TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


def record_buy(state_dir, *, token_id: str, buy_price: float,
               size_shares: float, kind: str) -> None:
    """Record or update the cost basis for a token.

    If the token already has a record, compute a new weighted-avg buy
    price. Otherwise insert a new row.
    """
    ensure_positions_cost_table(state_dir)
    now = datetime.now(timezone.utc).isoformat()
    con = _connect(state_dir)
    try:
        cur = con.execute(
            "SELECT buy_price, buy_size_shares FROM positions_cost WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        if cur is None:
            con.execute(
                """INSERT INTO positions_cost
                   (token_id, buy_price, buy_size_shares, buy_ts, kind)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_id, buy_price, size_shares, now, kind),
            )
        else:
            old_price = cur["buy_price"]
            old_size = cur["buy_size_shares"]
            new_size = old_size + size_shares
            new_avg = (old_price * old_size + buy_price * size_shares) / new_size
            con.execute(
                """UPDATE positions_cost
                   SET buy_price = ?, buy_size_shares = ?, buy_ts = ?
                   WHERE token_id = ?""",
                (new_avg, new_size, now, token_id),
            )
        con.commit()
    finally:
        con.close()


def fetch_position(state_dir, token_id: str) -> Optional[PositionRecord]:
    con = _connect(state_dir)
    try:
        row = con.execute(
            "SELECT * FROM positions_cost WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        if row is None:
            return None
        return PositionRecord(
            token_id=row["token_id"],
            buy_price=row["buy_price"],
            buy_size_shares=row["buy_size_shares"],
            buy_ts=row["buy_ts"],
            kind=row["kind"],
            last_evaluated_ts=row["last_evaluated_ts"],
            last_decision=row["last_decision"],
        )
    finally:
        con.close()


def fetch_all_directional_positions(state_dir) -> list[PositionRecord]:
    """Return all positions marked kind='directional' (skip arb buckets)."""
    ensure_positions_cost_table(state_dir)
    con = _connect(state_dir)
    try:
        rows = con.execute(
            "SELECT * FROM positions_cost WHERE kind = ? AND buy_size_shares > 0",
            ("directional",),
        ).fetchall()
        return [
            PositionRecord(
                token_id=r["token_id"],
                buy_price=r["buy_price"],
                buy_size_shares=r["buy_size_shares"],
                buy_ts=r["buy_ts"],
                kind=r["kind"],
                last_evaluated_ts=r["last_evaluated_ts"],
                last_decision=r["last_decision"],
            )
            for r in rows
        ]
    finally:
        con.close()


def mark_evaluated(state_dir, token_id: str, decision: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con = _connect(state_dir)
    try:
        con.execute(
            """UPDATE positions_cost
               SET last_evaluated_ts = ?, last_decision = ?
               WHERE token_id = ?""",
            (now, decision, token_id),
        )
        con.commit()
    finally:
        con.close()


def evaluate_position(
    pos: PositionRecord,
    *,
    current_price: float,
    hours_to_resolution: Optional[float] = None,
) -> ExitDecision:
    """Pure function: given a position and its current price, decide what to do.

    No I/O. Easy to unit-test.
    """
    pct = (current_price - pos.buy_price) / pos.buy_price if pos.buy_price > 0 else 0.0

    if pct >= TAKE_PROFIT_PCT:
        shares = pos.buy_size_shares * 0.5
        return ExitDecision(
            token_id=pos.token_id,
            decision="take_profit_50",
            current_price=current_price,
            buy_price=pos.buy_price,
            pct_change=pct,
            shares_to_sell=shares,
            reasoning=f"up {pct*100:.0f}% from {pos.buy_price:.3f} -> {current_price:.3f}, taking 50% profit",
        )
    if pct <= CUT_LOSS_PCT:
        return ExitDecision(
            token_id=pos.token_id,
            decision="cut_loss_100",
            current_price=current_price,
            buy_price=pos.buy_price,
            pct_change=pct,
            shares_to_sell=pos.buy_size_shares,
            reasoning=f"down {abs(pct)*100:.0f}% from {pos.buy_price:.3f} -> {current_price:.3f}, cutting 100% loss",
        )
    if (hours_to_resolution is not None
        and hours_to_resolution < NEAR_RESOLUTION_HOURS
        and current_price < WORTHLESS_PRICE):
        return ExitDecision(
            token_id=pos.token_id,
            decision="near_res_lost",
            current_price=current_price,
            buy_price=pos.buy_price,
            pct_change=pct,
            shares_to_sell=pos.buy_size_shares,
            reasoning=f"near resolution ({hours_to_resolution:.1f}h) and price ${current_price:.3f} effectively worthless",
        )
    return ExitDecision(
        token_id=pos.token_id,
        decision="hold",
        current_price=current_price,
        buy_price=pos.buy_price,
        pct_change=pct,
        shares_to_sell=0.0,
        reasoning=f"up {pct*100:+.1f}% from entry, within hold range",
    )


async def place_sell_order(
    token_id: str, price: float, size_shares: float,
) -> Optional[str]:
    """Place a SELL limit order on CLOB. Returns the order_id or None on error.

    Uses the MCP server's create_limit_order tool. price should be the
    best bid (so the order fills against the highest buyer).
    """
    try:
        result = await mcp_client.call_tool("create_limit_order", {
            "market_id": token_id,
            "side": "SELL",
            "price": price,
            "size": size_shares,
            "order_type": "GTC",
        })
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return None
        if not isinstance(result, dict):
            return None
        return result.get("orderID") or result.get("order_id") or result.get("id")
    except Exception:
        return None


async def fetch_token_book(token_id: str) -> dict:
    """Get the CLOB order book for a token. {bids: [{price,size}], asks: [...]}"""
    try:
        result = await mcp_client.call_tool("get_orderbook", {"token_id": token_id})
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return {"bids": [], "asks": []}
        if not isinstance(result, dict):
            return {"bids": [], "asks": []}
        return result
    except Exception:
        return {"bids": [], "asks": []}
async def verify_order_filled(order_id: str, token_id: str, *,
                              wait_seconds: float = 8.0) -> bool:
    """Check if a CLOB order has filled. Polls for a few seconds.

    Returns True if filled, False if still resting. The bot's caller
    should NOT count unfilled orders as completed trades.
    """
    if not order_id:
        return False
    try:
        import asyncio
        await asyncio.sleep(wait_seconds)
        result = await mcp_client.call_tool("get_order", {"order_id": order_id})
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return False
        if not isinstance(result, dict):
            return False
        status = result.get("status", "").upper()
        size_matched = float(result.get("size_matched", 0) or 0)
        if status in ("MATCHED", "FILLED"):
            return True
        if size_matched > 0:
            return True
        return False
    except Exception:
        return False


async def cancel_order(order_id: str) -> bool:
    """Cancel a CLOB order. Returns True if cancelled successfully."""
    if not order_id:
        return False
    try:
        result = await mcp_client.call_tool("cancel_order", {"order_id": order_id})
        return result is not None
    except Exception:
        return False


