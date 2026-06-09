"""Kill switch — single source of truth on whether the bot may place a trade."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from config import BotConfig
from state_manager import StateManager


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    smoke_test_active: bool

    def __bool__(self) -> bool:
        return self.allowed


class KillSwitch:
    def __init__(self, state_manager: StateManager, config: Optional[BotConfig] = None):
        self.sm = state_manager
        self.cfg = config or BotConfig.load()

    def check(self) -> Decision:
        s = self.sm.read()

        if s.get("halted"):
            return Decision(
                allowed=False,
                reason=f"HALTED — {s.get('halt_reason', 'no reason given')}",
                smoke_test_active=bool(s.get("smoke_test_active", False)),
            )

        if s.get("today_realized_pnl_usd", 0) <= -self.cfg.daily_loss_stop_usd:
            return Decision(
                allowed=False,
                reason=(f"daily loss stop hit "
                        f"(${abs(s.get('today_realized_pnl_usd', 0)):.2f} "
                        f"≥ ${self.cfg.daily_loss_stop_usd:.2f})"),
                smoke_test_active=bool(s.get("smoke_test_active", False)),
            )

        if s.get("today_trade_count", 0) >= self.cfg.daily_trade_count_max:
            return Decision(
                allowed=False,
                reason=(f"daily trade count cap hit "
                        f"({s.get('today_trade_count', 0)} "
                        f"≥ {self.cfg.daily_trade_count_max})"),
                smoke_test_active=bool(s.get("smoke_test_active", False)),
            )

        if s.get("smoke_test_active") and s.get("today_trade_count", 0) >= self.cfg.smoke_test_trade_cap:
            return Decision(
                allowed=False,
                reason=(f"smoke test active — only "
                        f"{self.cfg.smoke_test_trade_cap} trade(s) allowed today"),
                smoke_test_active=True,
            )

        if s.get("today_consecutive_failures", 0) >= self.cfg.consecutive_failures_halt:
            return Decision(
                allowed=False,
                reason=(f"{s.get('today_consecutive_failures', 0)} consecutive "
                        f"order failures — manual review required"),
                smoke_test_active=bool(s.get("smoke_test_active", False)),
            )

        return Decision(
            allowed=True,
            reason="ok",
            smoke_test_active=bool(s.get("smoke_test_active", False)),
        )
