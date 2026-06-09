"""Bot runner — main tick loop."""
from __future__ import annotations
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from bot.config import BotConfig
from bot.state_manager import StateManager
from bot.killswitch import KillSwitch
from bot.signal_filter import evaluate_market
from bot.notifier import Notifier
import bot.mcp_client as mcp_client


# Off-topic content filter: markets that the MCP "sports" tag includes
# but are actually pop culture / entertainment / non-game events.
_OFFTOPIC_KEYWORDS = (
    "album", "movie", "tv show", "tv series", "song", "single",
    "release party", "grammy", "oscar", "emmy", "celebrity",
    "kardashian", "twitter", "tiktok", "instagram", "youtube",
    "box office", "streaming", "spotify", "billboard",
    "nft", "crypto price", "bitcoin price", "eth price",
)


def _is_offtopic(market: dict) -> Optional[str]:
    q = (market.get("question") or market.get("slug") or "").lower()
    for kw in _OFFTOPIC_KEYWORDS:
        if kw in q:
            return f"off-topic content match: '{kw}'"
    return None


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _setup_logging(log_file) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file))
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)


class BotRunner:
    def __init__(self, config: Optional[BotConfig] = None):
        self.cfg = config or BotConfig.load()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        _setup_logging(self.cfg.log_file)
        self.log = logging.getLogger("polymarket_bot")
        self.sm = StateManager(state_dir=self.cfg.state_dir)
        self.ks = KillSwitch(state_manager=self.sm, config=self.cfg)
        self.notifier = Notifier(config=self.cfg)
        self._shutdown = False

    def _install_signal_handlers(self) -> None:
        def handle(sig, _frame):
            self.log.info(f"Received signal {sig}, shutting down after this tick")
            self._shutdown = True
        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    def _maybe_roll_day(self) -> None:
        s = self.sm.read()
        today = datetime.now(timezone.utc).date().isoformat()
        if s.get("today_utc_date") != today:
            self.log.info(f"Day roll: {s.get('today_utc_date')} → {today}")
            self.sm.update(
                today_utc_date=today,
                today_realized_pnl_usd=0.0,
                today_trade_count=0,
                today_consecutive_failures=0,
            )

    def _maybe_lift_smoke_test(self) -> None:
        s = self.sm.read()
        if not s.get("smoke_test_active"):
            return
        lift_at = s.get("smoke_test_lift_at_utc")
        if lift_at and datetime.now(timezone.utc) >= datetime.fromisoformat(lift_at):
            self.log.info("Smoke test window elapsed — lifting to full plan")
            self.sm.update(smoke_test_active=False)
            self.notifier._send("✅ *Smoke test lifted* — full plan active.")

    def _initialize_smoke_test(self) -> None:
        s = self.sm.read()
        if s.get("smoke_test_lift_at_utc") is None:
            lift_at = datetime.now(timezone.utc) + timedelta(
                hours=self.cfg.smoke_test_duration_hours
            )
            self.sm.update(
                smoke_test_active=True,
                smoke_test_lift_at_utc=lift_at.isoformat(),
            )
            self.log.info(f"Smoke test initialized — lifts at {lift_at.isoformat()}")

    async def _fetch_candidates(self) -> list[dict]:
        try:
            markets = await mcp_client.call_tool("get_sports_markets", {})
            if isinstance(markets, str):
                markets = json.loads(markets)
            if not isinstance(markets, list):
                self.log.warning(f"Unexpected get_sports_markets type: {type(markets)}")
                return []
        except Exception as e:
            self.log.error(f"get_sports_markets failed: {type(e).__name__}: {e}")
            return []

        candidates = []
        for m in markets:
            try:
                best_ask = m.get("bestAsk")
                end_date = m.get("endDate")
                liq = m.get("liquidityNum")
                if liq is None:
                    try:
                        liq = float(m.get("liquidity", 0) or 0)
                    except (TypeError, ValueError):
                        liq = 0.0
                condition_id = m.get("conditionId") or m.get("id")
                question = m.get("question") or m.get("slug") or ""
                candidates.append({
                    "question": question,
                    "slug": m.get("slug", ""),
                    "condition_id": condition_id,
                    "token_id": m.get("id"),
                    "best_ask": float(best_ask) if best_ask is not None else None,
                    "liquidity_usd": float(liq) if liq is not None else 0.0,
                    "end_date_utc": end_date,
                    "category": "sports",
                })
            except Exception as e:
                self.log.debug(f"Skipping malformed market: {e}")
                continue
        return candidates

    async def _place_order(self, market: dict, size_usd: float, price: float) -> Optional[str]:
        try:
            result = await mcp_client.call_tool("create_limit_order", {
                "market_id": market["condition_id"],
                "side": "BUY",
                "price": price,
                "size": size_usd,
                "order_type": "GTC",
            })
            if isinstance(result, str):
                result = json.loads(result)
            order_id = (result.get("orderID")
                        or result.get("order_id")
                        or result.get("id"))
            if not order_id:
                self.log.error(f"No order id in response: {result}")
                return None
            return str(order_id)
        except Exception as e:
            self.log.error(f"create_limit_order failed: {type(e).__name__}: {e}")
            return None

    async def tick(self) -> None:
        self._maybe_roll_day()
        self._maybe_lift_smoke_test()
        decision = self.ks.check()
        if not decision.allowed:
            self.log.info(f"Skipping tick — {decision.reason}")
            return

        candidates = await self._fetch_candidates()
        self.log.info(f"Fetched {len(candidates)} sports markets")

        placed = 0
        for market in candidates:
            off = _is_offtopic(market)
            if off:
                self.log.debug(f"Reject off-topic: {market.get('question','?')[:50]} — {off}")
                continue
            sig = evaluate_market(market, config=self.cfg)
            if not sig.accepted:
                self.log.debug(f"Reject signal: {market.get('question','?')[:50]} — {sig.reason}")
                continue
            size = min(sig.size_usd, self.cfg.per_trade_cap_usd)
            order_id = await self._place_order(
                market, size_usd=size, price=market["best_ask"]
            )
            if order_id:
                self.sm.update(
                    today_trade_count=self.sm.read().get("today_trade_count", 0) + 1,
                    last_trade_at_utc=datetime.now(timezone.utc).isoformat(),
                    today_consecutive_failures=0,
                )
                self.sm.record_trade(
                    condition_id=market.get("condition_id", ""),
                    token_id=market.get("token_id", ""),
                    side="BUY",
                    price=market["best_ask"],
                    size_usd=size,
                    status="OPEN",
                    order_id=order_id,
                )
                self.notifier.trade_opened(
                    market_question=market.get("question", ""),
                    side="BUY", price=market["best_ask"],
                    size_usd=size, order_id=order_id,
                )
                placed += 1
                break
            else:
                fail = self.sm.read().get("today_consecutive_failures", 0) + 1
                self.sm.update(today_consecutive_failures=fail)
                if fail >= self.cfg.consecutive_failures_halt:
                    reason = f"{fail} consecutive order failures"
                    self.log.error(reason)
                    self.sm.set_halt(True, reason=reason)
                    self.notifier.kill_switch_hit(reason)
                    return
        self.log.info(f"Tick complete — placed {placed} order(s)")

    async def run_forever(self) -> None:
        self._install_signal_handlers()
        self._initialize_smoke_test()
        self.log.info(f"Bot starting. Tick interval: {self.cfg.tick_interval_seconds}s")
        self.log.info(f"State dir: {self.cfg.state_dir}")
        while not self._shutdown:
            try:
                await self.tick()
            except Exception as e:
                self.log.exception(f"Tick crashed: {e}")
                self.notifier.error(f"tick crash: {type(e).__name__}: {e}")
            for _ in range(self.cfg.tick_interval_seconds):
                if self._shutdown:
                    break
                await asyncio.sleep(1)
        self.log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(BotRunner().run_forever())
