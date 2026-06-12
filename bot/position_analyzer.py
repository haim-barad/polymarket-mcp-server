"""
position_analyzer.py — Forward-looking analysis of current open positions.

Given the bot's open positions and current market state, compute:
  - Cost basis (from positions_cost table)
  - Current market price (from data-api or CLOB)
  - Implied probability (= current price for binary markets)
  - Resolution scenarios (payout if Yes / if No)
  - Expected value at current implied probability
  - Sensitivity: EV if implied prob is +/- 10% / +/- 20%
  - Comparison to current market exit value (mark-to-market)

Outputs:
  - Console summary
  - Vault markdown report at Vault-Personal/Finance/US/Polymarket/Position-Analysis.md
  - Optional Telegram digest

Does NOT:
  - Place any trades
  - Recommend or trigger smart-exit
  - Backtest the strategy

Usage:
  python -m bot.tools.position_analyzer
  python -m bot.tools.position_analyzer --telegram
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
GAMMA_API_MARKETS = "https://gamma-api.polymarket.com/markets"


def _http_get(url: str, timeout: float = 15.0) -> dict | list:
    """Simple HTTP GET. Returns parsed JSON or empty."""
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


@dataclass
class Position:
    """One open position, fetched from data-api."""
    condition_id: str
    asset: str  # CLOB token_id
    title: str
    outcome: str  # "Yes" or "No" etc
    size: float  # shares held
    current_value_usd: float
    current_price: float  # last-traded price (== current_value / size)
    avg_price: float  # data-api's own average entry price
    end_date: Optional[str] = None  # ISO date string

    @classmethod
    def from_data_api(cls, raw: dict) -> "Position":
        size = float(raw.get("size", 0) or 0)
        cv = float(raw.get("currentValue", 0) or 0)
        price = (cv / size) if size > 0 else 0.0
        avg = float(raw.get("avgPrice", 0) or 0)
        return cls(
            condition_id=raw.get("conditionId", ""),
            asset=raw.get("asset", ""),
            title=raw.get("title", "?"),
            outcome=raw.get("outcome", "?"),
            size=size,
            current_value_usd=cv,
            current_price=price,
            avg_price=avg,
            end_date=raw.get("endDate"),
        )


@dataclass
class CostBasis:
    """Cost basis from positions_cost table."""
    buy_price: float
    buy_size_shares: float
    kind: str  # "directional" or "arb_bucket"
    last_evaluated_ts: Optional[str]
    last_decision: Optional[str]

    @property
    def total_cost(self) -> float:
        return self.buy_price * self.buy_size_shares


def fetch_open_positions(wallet_address: str, min_value: float = 0.01) -> list[Position]:
    """Fetch current open positions from data-api."""
    url = f"{DATA_API_POSITIONS}?user={wallet_address}&sizeThreshold={min_value}"
    try:
        data = _http_get(url)
    except Exception as e:
        print(f"[analyzer] data-api fetch failed: {e}")
        return []
    if not isinstance(data, list):
        return []
    return [Position.from_data_api(p) for p in data]


def fetch_cost_basis(state_dir: Path) -> dict[str, CostBasis]:
    """Read the positions_cost table from the bot's SQLite ledger."""
    import sqlite3
    db = state_dir / "bot.db"
    if not db.exists():
        return {}
    out: dict[str, CostBasis] = {}
    con = sqlite3.connect(str(db), timeout=5)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute(
            "SELECT token_id, buy_price, buy_size_shares, kind, last_evaluated_ts, last_decision "
            "FROM positions_cost WHERE buy_size_shares > 0"
        ):
            out[r["token_id"]] = CostBasis(
                buy_price=r["buy_price"],
                buy_size_shares=r["buy_size_shares"],
                kind=r["kind"],
                last_evaluated_ts=r["last_evaluated_ts"],
                last_decision=r["last_decision"],
            )
    finally:
        con.close()
    return out


def fetch_market_metadata(condition_id: str) -> dict:
    """Get the market's endDate, question, and current pricing from gamma.

    Returns dict with: question, endDate, clobTokenIds, outcomes.
    """
    try:
        url = f"{GAMMA_API_MARKETS}?condition_ids={condition_id}"
        data = _http_get(url)
        if isinstance(data, list) and data:
            m = data[0]
            # Parse clobTokenIds (it's a JSON string) and outcomes (also JSON string)
            try:
                m["_clobTokenIds_parsed"] = json.loads(m.get("clobTokenIds", "[]"))
            except Exception:
                m["_clobTokenIds_parsed"] = []
            try:
                m["_outcomes_parsed"] = json.loads(m.get("outcomes", "[]"))
            except Exception:
                m["_outcomes_parsed"] = []
            return m
    except Exception as e:
        print(f"[analyzer] gamma fetch failed for {condition_id[:12]}: {e}")
    return {}


def analyze_position(pos: Position, cost: CostBasis, market: dict) -> dict:
    """Compute the forward-looking analysis for one position.

    Returns a dict with:
      - cost_basis (total $)
      - current_value ($)
      - pnl_if_held_to_resolution_yes (the position's payout if YES wins)
      - pnl_if_held_to_resolution_no (payout if NO wins; usually $0)
      - implied_prob_yes (from current price, 0-1)
      - ev_at_implied_prob (probability-weighted expected P&L)
      - ev_sensitivity (EV if prob +/- 10% / +/- 20%)
      - days_to_resolution (int)
      - recommendation (just "HOLD" or "REVIEW" - no auto-decision)
    """
    # Find the corresponding YES/NO token IDs and their prices.
    # The data-api gives us ONE side (the side we hold).
    # For binary markets: payout at resolution = 1.0 per share if our side wins.
    cost_basis = cost.total_cost
    current_value = pos.current_value_usd
    pnl_now = current_value - cost_basis  # mark-to-market (if sold now at last)

    # End date
    end_str = market.get("endDate", "")
    days_to_resolution: Optional[int] = None
    if end_str:
        try:
            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            delta = dt - datetime.now(timezone.utc)
            days_to_resolution = max(0, int(delta.total_seconds() // 86400))
        except Exception:
            pass

    # Implied probability of YES side. For binary, this is just the
    # displayed last-traded price. We use 0.5 as fallback if unknown.
    implied_prob_yes = pos.current_price if pos.size > 0 else 0.5
    # If the held outcome is "No", implied_prob_no = price; implied_prob_yes = 1 - price
    if pos.outcome.lower().startswith("no"):
        # We're holding NO. Implied prob NO = pos.current_price.
        # Implied prob YES = 1 - pos.current_price.
        # Payout at resolution: 1.0 per share if NO wins, 0 if YES wins.
        p_yes = 1.0 - pos.current_price
        p_no = pos.current_price
        pnl_if_yes_wins = -cost_basis   # we lose our cost
        pnl_if_no_wins = pos.size - cost_basis  # we win $1/share minus cost
    else:
        # Holding YES
        p_yes = pos.current_price
        p_no = 1.0 - pos.current_price
        pnl_if_yes_wins = pos.size - cost_basis
        pnl_if_no_wins = -cost_basis

    # EV at current implied probability (per share)
    ev_per_share = p_yes * pnl_if_yes_wins / max(pos.size, 1e-9) + p_no * pnl_if_no_wins / max(pos.size, 1e-9)
    ev_at_implied = ev_per_share * pos.size

    # Sensitivity: how does EV change if our probability estimate is off?
    # We don't have a model for true prob - we use the implied prob as our
    # best estimate. The sensitivity shows EV at +/- 10% / +/- 20% in p_yes.
    sensitivity = {}
    for shift in (-0.20, -0.10, 0, 0.10, 0.20):
        p_yes_adj = max(0.0, min(1.0, p_yes + shift))
        p_no_adj = 1.0 - p_yes_adj
        ev_adj_per_share = (
            p_yes_adj * (pnl_if_yes_wins / max(pos.size, 1e-9))
            + p_no_adj * (pnl_if_no_wins / max(pos.size, 1e-9))
        )
        sensitivity[f"prob_{'+'if shift>=0 else ''}{int(shift*100)}pct"] = ev_adj_per_share * pos.size

    # Recommendation: no auto-decision. Just flag if there's something
    # notable (large mark-to-market loss, near resolution, very wide spread)
    notes = []
    if pnl_now < -cost_basis * 0.30:
        notes.append(f"mark-to-market loss {pnl_now:.2f} ({pnl_now/cost_basis*100:.0f}% of cost)")
    if days_to_resolution is not None and days_to_resolution <= 1:
        notes.append(f"resolves in {days_to_resolution} day(s)")
    if cost.kind == "arb_bucket":
        notes.append("arb bucket - hold to resolution is the strategy")

    return {
        "title": pos.title,
        "outcome": pos.outcome,
        "size": pos.size,
        "current_price": pos.current_price,
        "current_value_usd": current_value,
        "cost_basis_usd": cost_basis,
        "pnl_mark_to_market": pnl_now,
        "kind": cost.kind,
        "data_api_avg_price": pos.avg_price,
        "days_to_resolution": days_to_resolution,
        "implied_prob_yes": p_yes,
        "pnl_if_yes_wins": pnl_if_yes_wins,
        "pnl_if_no_wins": pnl_if_no_wins,
        "ev_at_implied_prob": ev_at_implied,
        "ev_sensitivity": sensitivity,
        "notes": notes,
    }


def render_markdown_report(analyses: list[dict], as_of: datetime) -> str:
    """Format the analyses as a markdown report suitable for the vault."""
    lines: list[str] = []
    lines.append(f"# Position Analysis — {as_of.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("Forward-looking analysis of currently open positions.")
    lines.append("Outputs probability-weighted expected P&L and sensitivity to probability estimates.")
    lines.append("")
    lines.append("**Does NOT trigger trades. Does NOT recommend action. Read the numbers, decide.**")
    lines.append("")

    # Summary
    total_cost = sum(a["cost_basis_usd"] for a in analyses)
    total_value = sum(a["current_value_usd"] for a in analyses)
    total_pnl = total_value - total_cost
    total_ev = sum(a["ev_at_implied_prob"] for a in analyses)
    lines.append("## Summary")
    lines.append(f"- Positions: {len(analyses)}")
    lines.append(f"- Total cost basis: ${total_cost:.2f}")
    lines.append(f"- Current value (mark-to-market): ${total_value:.2f}")
    lines.append(f"- Mark-to-market P&L: ${total_pnl:+.2f} ({total_pnl/total_cost*100:+.1f}%)")
    lines.append(f"- Probability-weighted EV if all held to resolution: ${total_ev:+.2f}")
    lines.append("")

    # Per-position
    lines.append("## Per-Position Analysis")
    lines.append("")
    for a in analyses:
        lines.append(f"### {a['title'][:80]}")
        lines.append("")
        lines.append(f"- **Outcome:** {a['outcome']} | **Size:** {a['size']:.2f} sh | "
                     f"**Kind:** {a['kind']}")
        lines.append(f"- **Current price:** ${a['current_price']:.3f} | "
                     f"**Implied P(YES):** {a['implied_prob_yes']*100:.1f}%")
        lines.append(f"- **Cost basis:** ${a['cost_basis_usd']:.2f} | "
                     f"**Current value:** ${a['current_value_usd']:.2f} | "
                     f"**Mark-to-market P&L:** ${a['pnl_mark_to_market']:+.2f}")
        if a.get("data_api_avg_price") is not None and a["kind"] != "data-api-derived":
            lines.append(f"  (data-api avgPrice: ${a['data_api_avg_price']:.3f} × {a['size']:.2f} sh = "
                         f"${a['data_api_avg_price'] * a['size']:.2f} — cross-check)")
        if a['days_to_resolution'] is not None:
            lines.append(f"- **Days to resolution:** {a['days_to_resolution']}")
        lines.append(f"- **P&L if YES wins:** ${a['pnl_if_yes_wins']:+.2f}")
        lines.append(f"- **P&L if NO wins:** ${a['pnl_if_no_wins']:+.2f}")
        lines.append(f"- **EV at implied prob:** ${a['ev_at_implied_prob']:+.2f}")
        lines.append("")
        lines.append("**EV sensitivity to probability estimate:**")
        lines.append("")
        lines.append("| P(YES) shift | EV |")
        lines.append("|---|---|")
        for label, ev in a["ev_sensitivity"].items():
            lines.append(f"| {label} | ${ev:+.2f} |")
        lines.append("")
        if a["notes"]:
            lines.append("**Notes:**")
            for note in a["notes"]:
                lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)


def run_analysis(state_dir: Path, wallet_address: str) -> list[dict]:
    """Main entry point: fetch data, compute analyses, return list of dicts."""
    positions = fetch_open_positions(wallet_address)
    cost_basis = fetch_cost_basis(state_dir)
    analyses: list[dict] = []

    for pos in positions:
        # Find the matching cost basis. The data-api's `asset` field is the
        # CLOB token_id, which is what positions_cost uses.
        # Try asset first, then conditionId (some older entries used the cid).
        cost = None
        source = "bot ledger"
        if pos.asset and pos.asset in cost_basis:
            cost = cost_basis[pos.asset]
        elif pos.condition_id and pos.condition_id in cost_basis:
            cost = cost_basis[pos.condition_id]
            source = "bot ledger (cid fallback)"
        if cost is None:
            # Fallback: use data-api's avgPrice as the cost basis. This is
            # computed by data-api and is more accurate than guessing.
            source = "data-api avgPrice"
            cost = CostBasis(
                buy_price=pos.avg_price,
                buy_size_shares=pos.size,
                kind="data-api-derived",
                last_evaluated_ts=None,
                last_decision=None,
            )

        # Fetch market metadata for endDate
        market = fetch_market_metadata(pos.condition_id)
        analyses.append(analyze_position(pos, cost, market))

    return analyses


def post_to_telegram(report: str) -> bool:
    """Post a short summary to Telegram. Returns success."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from bot.telegram_notify import notify
        # Just the summary, not the full report
        lines = report.split("\n")
        summary = "\n".join([l for l in lines if l.startswith("#") or l.startswith("-") or l.startswith("|")][:25])
        return notify(f"Position analysis:\n\n{summary[:3000]}")
    except Exception as e:
        print(f"[analyzer] telegram post failed: {e}")
        return False


def write_vault_report(report: str, vault_path: Path) -> None:
    """Write the report to the vault."""
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    with open(vault_path, "w") as f:
        f.write(report)
    print(f"[analyzer] wrote {vault_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--wallet", type=str, default="0x55183ffA1a169C2bc92d8b5E9B5Aeb444A637023")
    parser.add_argument("--vault-path", type=Path,
                        default=Path("/Users/haimbarad/Library/Mobile Documents/com~apple~CloudDocs/Vaults/Vault-Personal/Finance/US/Polymarket/Position-Analysis.md"))
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    state_dir = args.state_dir or Path("/Users/haimbarad/.hermes/polymarket-bot/state")
    analyses = run_analysis(state_dir, args.wallet)
    as_of = datetime.now(timezone.utc)
    report = render_markdown_report(analyses, as_of)

    # Print summary
    total_cost = sum(a["cost_basis_usd"] for a in analyses)
    total_value = sum(a["current_value_usd"] for a in analyses)
    print(f"\n{len(analyses)} positions analyzed")
    print(f"  Cost basis: ${total_cost:.2f}")
    print(f"  Current value: ${total_value:.2f}")
    print(f"  Mark-to-market P&L: ${total_value - total_cost:+.2f}")

    write_vault_report(report, args.vault_path)
    if args.telegram:
        post_to_telegram(report)
