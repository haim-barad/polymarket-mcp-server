"""Telegram notifier — wraps the existing telegram_notify helper."""
from __future__ import annotations
import logging
from typing import Optional

from config import BotConfig

log = logging.getLogger("polymarket_bot.notifier")

try:
    from telegram_notify import notify as _tg_notify
    _IMPORT_OK = True
except ImportError:
    _IMPORT_OK = False


class Notifier:
    def __init__(self, config: Optional[BotConfig] = None):
        self.cfg = config or BotConfig.load()

    def _send(self, text: str, silent: bool = False) -> bool:
        if not self.cfg.telegram_alerts_enabled:
            log.info(f"[telegram disabled] {text}")
            return False
        if not _IMPORT_OK:
            log.warning(f"[telegram module missing] {text}")
            return False
        try:
            return bool(_tg_notify(text, silent=silent))
        except Exception as e:
            log.warning(f"[telegram send failed] {type(e).__name__}: {e}")
            return False

    def trade_opened(self, *, market_question: str, side: str,
                     price: float, size_usd: float, order_id: str) -> None:
        text = (f"📈 *{side}* ${size_usd:.2f} @ ${price:.3f}\n"
                f"Market: {market_question[:80]}\n"
                f"Order: `{order_id}`")
        self._send(text)

    def kill_switch_hit(self, reason: str) -> None:
        self._send(f"⛔ *KILL SWITCH*\n{reason}")

    def error(self, err: str) -> None:
        self._send(f"❌ *Bot error* — {err}")

    def daily_summary(self, *, trades: int, pnl_usd: float,
                      open_positions: int, exposure_usd: float,
                      smoke_test_active: bool) -> None:
        flag = "  ⚠️ smoke test" if smoke_test_active else ""
        text = (f"📊 *Daily EOD summary* (18:00 UTC){flag}\n"
                f"• Trades: {trades}\n"
                f"• Realized P&L: ${pnl_usd:+.2f}\n"
                f"• Open positions: {open_positions} (${exposure_usd:.2f})")
        self._send(text, silent=True)
