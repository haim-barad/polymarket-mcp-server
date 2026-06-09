"""Daily EOD summary — runs at 18:00 UTC.

Posts a one-line summary to Telegram: trades today, realized P&L,
open positions, smoke-test status. silent=True (no notification buzz).
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

# Make bot package importable when called as `python -m bot.daily_summary`
# (pyproject.toml has pythonpath=["bot"] for pytest, but `python -m` from
# the repo root needs the parent dir on sys.path to find `bot/`)
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.config import BotConfig
from bot.state_manager import StateManager
from bot.notifier import Notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("polymarket_bot.daily_summary")


def main() -> int:
    cfg = BotConfig.load()
    sm = StateManager(state_dir=cfg.state_dir)
    notifier = Notifier(config=cfg)

    trades = sm.trades_today()
    pnl = sm.realized_pnl_today()
    s = sm.read()

    notifier.daily_summary(
        trades=len(trades),
        pnl_usd=pnl,
        open_positions=s.get("open_position_count", 0),
        exposure_usd=s.get("open_exposure_usd", 0.0),
        smoke_test_active=bool(s.get("smoke_test_active", False)),
    )
    log.info(f"Daily summary sent — {len(trades)} trades, P&L ${pnl:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
