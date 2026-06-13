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
import bot.onchain_reconcile as onchain_reconcile
import bot.arb_detector as arb_detector
import bot.arb_executor as arb_executor
import bot.smart_exit as smart_exit


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


def _should_alert_over_cap(
    prev_exposure: Optional[float],
    curr_exposure: float,
    cap: float,
    latched: bool,
) -> tuple[bool, bool]:
    """Edge-triggered over-cap alert decision.

    Returns (should_alert, new_latched). Fires the alert ONLY on the rising
    edge — the transition from at-or-under the cap to over the cap. Stays
    silent while continuously over cap. Re-arms (returns latched=False) when
    exposure drops back to at-or-under the cap, so the next rising edge will
    alert again.

    Haim's spec (2026-06-12): "should only appear once when it exceeds the
    cap and not repeated until the next time it goes below the cap and then
    re-exceeds the cap."

    The previous `cap_alert_sent_today` flag fired once per UTC day regardless
    of transitions — if Haim took action to reduce exposure but it was still
    over cap at midnight, the next day would re-fire. The previous
    `cap_alert_sent_tick` flag reset every tick and could fire on consecutive
    ticks (e.g. tick N alerts, exposure stays over cap, tick N+1 reset, tick
    N+1 alerts again). Both are level-triggered; this is edge-triggered.
    """
    if prev_exposure is None:
        prev_exposure = 0.0
    over_now = curr_exposure > cap
    over_prev = prev_exposure > cap
    if over_now and not over_prev:
        # Rising edge: fire alert, latch so we don't re-fire while over cap
        return True, True
    if not over_now and latched:
        # Falling edge: re-arm, no alert
        return False, False
    # No transition: stay in current latch state, no alert
    return False, latched


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
            # Send a Telegram digest of yesterday's arb findings
            self._send_daily_arb_digest()
            self.sm.update(
                today_utc_date=today,
                today_realized_pnl_usd=0.0,
                today_trade_count=0,
                today_consecutive_failures=0,
                cap_alert_sent_tick=False,
                cap_alert_sent_today=False,
                pre_trade_cap_logged_today=False,
            )

    def _send_daily_arb_digest(self) -> None:
        """Pull all strategy_events from the past 24h, summarize for Haim.
        Sends to Telegram via the notifier. Cheap and one-shot per day.
        """
        try:
            with self.sm._connect() as con:
                rows = con.execute(
                    """SELECT strategy, event_title, action, detail_json
                       FROM strategy_events
                       WHERE substr(ts_utc, 1, 10) >= date('now', '-1 day')
                       ORDER BY id"""
                ).fetchall()
            if not rows:
                return
            # Aggregate: count by event_title + action
            from collections import Counter
            detected = Counter()
            executed = Counter()
            for r in rows:
                r = dict(r)
                title = (r.get("event_title") or "?")[:50]
                if r.get("action") == "detected":
                    detected[title] += 1
                elif r.get("action") == "trade_placed":
                    executed[title] += 1
            if not detected and not executed:
                return
            lines = ["*Arb digest (last 24h)*"]
            for title, count in detected.most_common(10):
                ex = executed.get(title, 0)
                lines.append(f"* {title}  detected={count}  executed={ex}")
            text = "\n".join(lines)
            self.notifier._send(text, silent=True)
        except Exception as e:
            self.log.warning(f"daily arb digest failed: {type(e).__name__}: {e}")

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

    async def _fetch_events(self) -> list[dict]:
        """Fetch active events from gamma-api (group-level, not market-level).

        Pages through up to 4 pages (400 events) to get good coverage of
        the multi-outcome event space. The detector needs a meaningful
        sample size to find arbs — 100 events only catches the most
        obvious ones.
        """
        import urllib.request
        all_events = []
        loop = asyncio.get_event_loop()
        for offset in (0, 100, 200, 300):
            url = f"https://gamma-api.polymarket.com/events?closed=false&limit=100&offset={offset}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
                data = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=20).read())
                events = json.loads(data)
                if isinstance(events, list):
                    all_events.extend(events)
                if len(events) < 100:
                    break  # last page
            except Exception as e:
                self.log.warning(f"_fetch_events offset={offset} failed: {type(e).__name__}: {e}")
                break
        return all_events

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

    async def _scan_and_execute_arbs(self) -> list[dict]:
        """Run the multi-outcome arb detector, size and place orders.

        Called once per tick. Returns a list of trade records placed
        (so the caller can log them and update the daily trade count).
        """
        events = await self._fetch_events()
        if not events:
            self.log.info("Arb scan: 0 events fetched from gamma-api")
            return []
        self.log.info(f"Arb scan: fetched {len(events)} events from gamma-api")
        opportunities = arb_detector.scan_events(events)
        if not opportunities:
            self.log.debug(f"Arb scan: 0 opportunities in {len(events)} events")
            return []
        # Cap at top-3 arbs per tick (don't flood the orderbook on a busy day)
        opportunities = opportunities[:3]
        self.log.info(
            f"Arb scan: {len(opportunities)} opportunities in {len(events)} events: "
            + ", ".join(f"{o.event_title[:30]}({o.gross_return_pct*100:.0f}%)" for o in opportunities)
        )
        # Record each detection
        for opp in opportunities:
            self.sm.record_strategy_event(
                strategy="multi_outcome_arb",
                event_title=opp.event_title,
                action="detected",
                detail={"sum_yes": opp.sum_yes, "deviation": opp.deviation,
                        "n_buckets": opp.n_buckets, "gross_return_pct": opp.gross_return_pct},
            )
        all_trades: list[dict] = []
        for opp in opportunities:
            enriched = await arb_executor.enrich_opportunity(opp)
            per_bucket_usd, buyable = arb_executor.size_for_opportunity(
                enriched, cap_usd=self.cfg.per_trade_cap_usd,
                total_cap_usd=self.cfg.total_open_exposure_usd,
            )
            if per_bucket_usd < 0.50 or not buyable:
                self.log.info(
                    f"Arb {opp.event_title[:40]}: skip — too thin "
                    f"(size ${per_bucket_usd:.2f}, {len(buyable)} buyable buckets)"
                )
                continue
            # Pre-trade cap check: how much would this cost?
            est_cost = per_bucket_usd * len(buyable)
            from bot import onchain_reconcile as _oc_for_arb
            fresh = _oc_for_arb.fetch_positions(self.cfg.proxy_address, min_value=0.0)
            current = sum(p.get("current_value", 0) for p in fresh)
            # Use headroom-aware sizing: shrink per_bucket to fit under
            # (cap - current_exposure). Otherwise a $29 wallet with a
            # $50 cap can never execute a $25 arb because $29 + $25 > $50.
            available_headroom = max(0.0, self.cfg.total_open_exposure_usd - current)
            max_per_bucket_for_headroom = available_headroom / max(1, len(buyable))
            if per_bucket_usd > max_per_bucket_for_headroom:
                shrunk = max_per_bucket_for_headroom
                if shrunk < 0.50:
                    self.log.info(
                        f"Arb {opp.event_title[:40]}: skip — only ${available_headroom:.2f} "
                        f"headroom, need ${per_bucket_usd * len(buyable):.2f}"
                    )
                    continue
                self.log.info(
                    f"Arb {opp.event_title[:40]}: shrunk per_bucket ${per_bucket_usd:.2f} → "
                    f"${shrunk:.2f} to fit headroom (current=${current:.2f}, cap=${self.cfg.total_open_exposure_usd:.2f})"
                )
                per_bucket_usd = shrunk
            est_cost = per_bucket_usd * len(buyable)
            if current + est_cost > self.cfg.total_open_exposure_usd:
                # Only fire log+continue once per day per arb; reset on
                # day-roll. Without this, the same arb re-logs every 5
                # minutes while we're over cap.
                if not self.sm.read().get("pre_trade_cap_logged_today", False):
                    self.log.info(
                        f"Arb {opp.event_title[:40]}: pre-trade cap would push exposure "
                        f"${current + est_cost:.2f} > cap ${self.cfg.total_open_exposure_usd:.2f} "
                        f"(won't re-log today)"
                    )
                    self.sm.update(pre_trade_cap_logged_today=True)
                continue
            # Execute
            def on_trade(trade):
                self.sm.record_strategy_event(
                    strategy="multi_outcome_arb",
                    event_title=opp.event_title,
                    action="trade_placed",
                    detail={"bucket": trade.get("bucket_title"),
                            "size_usd": trade.get("size_usd"),
                            "price": trade.get("price")},
                )
            trades = await arb_executor.execute_arb(
                enriched, on_trade_placed=on_trade,
                cap_usd=self.cfg.per_trade_cap_usd,
            )
            if trades:
                all_trades.extend(trades)
                self.log.info(
                    f"Arb placed: {opp.event_title[:50]} → {len(trades)} buckets, "
                    f"~${sum(t['size_usd'] for t in trades):.2f} deployed"
                )
                # One arb per tick is plenty — don't queue more
                break
        return all_trades


    async def _run_smart_exit(self) -> list[dict]:
        """Evaluate open directional positions and exit ones that meet
        take-profit or cut-loss criteria. Returns a list of trade records
        placed (so the caller can log them and update daily trade count).
        """
        try:
            smart_exit.ensure_positions_cost_table(self.cfg.state_dir)
        except Exception as e:
            self.log.warning(f"smart_exit table init failed: {e}")
            return []
        positions = smart_exit.fetch_all_directional_positions(self.cfg.state_dir)
        if not positions:
            return []
        self.log.debug(f"Smart exit: evaluating {len(positions)} directional positions")
        trades = []
        # Skip positions that were just evaluated for the same
        # decision (within SKIP_RETRY_MINUTES). This prevents the bot
        # from retrying an unfilled sell on every tick when the orderbook
        # is too thin to fill at the placed price.
        from datetime import datetime, timezone, timedelta
        SKIP_RETRY_MINUTES = 30
        now = datetime.now(timezone.utc)
        for pos in positions:
            if pos.last_evaluated_ts:
                try:
                    last_eval = datetime.fromisoformat(pos.last_evaluated_ts)
                    if (now - last_eval) < timedelta(minutes=SKIP_RETRY_MINUTES):
                        # Recently evaluated. Skip unless the position
                        # has materially changed.
                        continue
                except Exception:
                    pass
            try:
                # Fetch current price from CLOB
                book = await smart_exit.fetch_token_book(pos.token_id)
                asks = book.get("asks", [])
                bids = book.get("bids", [])
                # Use best bid as the current "fair" price for an exit decision
                # (we'd SELL at the best bid, not at the best ask)
                if not bids:
                    continue
                # Best bid is highest price (sort desc)
                best_bid = float(sorted(bids, key=lambda b: -float(b.get("price", 0)))[0].get("price"))
                # Hours to resolution: not implemented in v1 (would need
                # to look up the endDate of the market via CLOB). Skip
                # the near_res_lost check for now.
                decision = smart_exit.evaluate_position(
                    pos, current_price=best_bid
                )
                if decision.decision == "hold":
                    continue
                self.log.info(
                    f"Smart exit: {decision.decision} on {pos.token_id[:12]}... "
                    f"({decision.pct_change*100:+.0f}% from entry) — {decision.reasoning}"
                )
                # Place SELL order at the best bid (limit order)
                order_id = await smart_exit.place_sell_order(
                    pos.token_id, best_bid, decision.shares_to_sell
                )
                if not order_id:
                    self.log.warning(f"smart_exit: order placement failed for {pos.token_id[:12]}")
                    continue
                # Verify the order actually filled. The CLOB orderbook
                # is mostly dust at the extremes; limit orders at the
                # displayed best bid often don't fill. Without this
                # check, we'd increment today_trade_count and record
                # the trade even though no shares changed hands.
                filled = await smart_exit.verify_order_filled(
                    order_id, pos.token_id, wait_seconds=6.0
                )
                if not filled:
                    self.log.info(
                        f"smart_exit: order {order_id[:16]} not filled, "
                        f"cancelling (will retry next tick at lower price)"
                    )
                    await smart_exit.cancel_order(order_id)
                    smart_exit.mark_evaluated(
                        self.cfg.state_dir, pos.token_id,
                        f"{decision.decision}_unfilled"
                    )
                    continue
                # Order filled: now safe to record as a trade and update
                # state. (Reordered 2026-06-13: prior to this, the bot
                # would record an unfilled order as a completed trade
                # and re-enter on the next tick, leading to position
                # inflation without real exposure decrease.)
                self.sm.record_strategy_event(
                    strategy="smart_exit",
                    event_title=pos.token_id[:20],
                    action=decision.decision,
                    detail={"shares": decision.shares_to_sell,
                            "price": best_bid,
                            "pct_change": decision.pct_change,
                            "reasoning": decision.reasoning,
                            "order_id": order_id},
                )
                trades.append({
                    "token_id": pos.token_id,
                    "side": "SELL",
                    "price": best_bid,
                    "size_shares": decision.shares_to_sell,
                    "order_id": order_id,
                    "decision": decision.decision,
                })
                smart_exit.mark_evaluated(self.cfg.state_dir, pos.token_id, decision.decision)
            except Exception as e:
                self.log.warning(f"smart_exit error for {pos.token_id[:12]}: {e}")
        if trades:
            self.log.info(f"Smart exit: placed {len(trades)} sell orders")
        return trades

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
        # Reset the once-per-tick Telegram cap-alert flag so the user
        # gets pinged if the cap is hit again on a later tick.
        # NOTE (2026-06-12): this flag is now legacy. Both cap-alert sites
        # below are edge-triggered via `_should_alert_over_cap` and the
        # `over_cap_latched` / `pre_trade_over_cap_latched` state fields.
        # The latch persists across ticks and only resets when exposure
        # drops back below the cap — see Haim's spec: "should only appear
        # once when it exceeds the cap and not repeated until the next
        # time it goes below the cap and then re-exceeds the cap."
        self.sm.update(cap_alert_sent_tick=False)
        # 1. Reconcile on-chain exposure FIRST. This is the source of
        #    truth for the cap check. Without this, new BUY orders can
        #    be placed before we know the actual exposure.
        #    (Reordered 2026-06-13: previously reconcile ran at end
        #    of tick, after arb-scan, so a buy could fire when we were
        #    already over cap. Bug observed 2026-06-12.)
        over_cap = False
        try:
            over_cap = self._reconcile_and_check_cap()
        except Exception as e:
            self.log.warning(f"start-of-tick reconcile failed: {type(e).__name__}: {e}")
        # 2. Try the multi-outcome arb scanner ONLY if we're under cap.
        arb_trades: list[dict] = []
        if not over_cap:
            try:
                arb_trades = await self._scan_and_execute_arbs()
                for trade in arb_trades:
                    self.sm.update(
                        today_trade_count=self.sm.read().get("today_trade_count", 0) + 1,
                    )
                    self.sm.record_trade(
                        condition_id=trade.get("token_id", ""),
                        token_id=trade.get("token_id", ""),
                        side="BUY",
                        price=trade.get("price", 0),
                        size_usd=trade.get("size_usd", 0),
                        status="OPEN",
                        order_id=trade.get("order_id", ""),
                    )
                    self.notifier.trade_opened(
                        market_question=trade.get("event_title", "?") + " — " + trade.get("bucket_title", ""),
                        side="BUY",
                        price=trade.get("price", 0),
                        size_usd=trade.get("size_usd", 0),
                        order_id=trade.get("order_id", ""),
                    )
            except Exception as e:
                self.log.warning(f"arb scan failed: {type(e).__name__}: {e}")
            if arb_trades:
                self.log.info(f"Arb tick: {len(arb_trades)} trades placed")
        # 2. Smart exit: evaluate directional positions for take-profit / cut-loss
        try:
            exit_trades = await self._run_smart_exit()
            for trade in exit_trades:
                self.sm.update(
                    today_trade_count=self.sm.read().get("today_trade_count", 0) + 1,
                )
                self.sm.record_trade(
                    condition_id=trade.get("token_id", ""),
                    token_id=trade.get("token_id", ""),
                    side="SELL",
                    price=trade.get("price", 0),
                    size_usd=trade.get("price", 0) * trade.get("size_shares", 0),
                    status="OPEN",
                    order_id=trade.get("order_id", ""),
                )
                self.notifier.trade_opened(
                    market_question=f"smart exit {trade.get('decision', '?')}",
                    side="SELL",
                    price=trade.get("price", 0),
                    size_usd=trade.get("price", 0) * trade.get("size_shares", 0),
                    order_id=trade.get("order_id", ""),
                )
            if exit_trades:
                self.log.info(f"Smart exit: {len(exit_trades)} sell orders placed")
        except Exception as e:
            self.log.warning(f"smart exit failed: {type(e).__name__}: {e}")
        # Reconcile against on-chain truth FIRST, before any kill switch
        # check. Without this, the bot can be fooled by a stale state file
        # that says "no open positions" when the wallet actually has $30+
        # of exposure from previous runs whose cancel-all didn't unwind
        # filled positions. (Added 2026-06-09 after the smoke-test
        # duplication incident.)
        wallet = self.cfg.proxy_address
        try:
            # Read the pre-reconcile exposure to detect the rising edge
            # (transition from <= cap to > cap). The previous level-triggered
            # `cap_alert_sent_today` flag would re-fire on every UTC day
            # rollover while still over cap, and `cap_alert_sent_tick` would
            # re-fire on every tick after a reset. Haim's spec: only fire
            # on the rising edge, then stay silent until exposure falls back
            # below the cap, then re-arm.
            # NOTE: onchain_reconcile.reconcile() updates open_exposure_usd
            # in state, so we read prev_exposure FIRST, then call reconcile,
            # then read the new value from the returned summary (not from
            # state, which would now reflect the post-reconcile value).
            prev_exposure = self.sm.read().get("open_exposure_usd", 0.0) or 0.0
            prev_latched = bool(self.sm.read().get("over_cap_latched", False))
            summary = onchain_reconcile.reconcile(self.sm, wallet, config=self.cfg)
            self.sm.update(onchain_positions_by_market=summary["positions_by_market"])
            should_alert, new_latched = _should_alert_over_cap(
                prev_exposure=prev_exposure,
                curr_exposure=summary["open_exposure_usd"],
                cap=self.cfg.total_open_exposure_usd,
                latched=prev_latched,
            )
            if should_alert:
                self.notifier.error(
                    f"On-chain exposure ${summary['open_exposure_usd']:.2f} "
                    f"exceeds cap ${self.cfg.total_open_exposure_usd:.2f}. "
                    f"Halting until manual reconciliation. "
                    f"(will not re-alert until exposure drops back under cap)"
                )
            # Persist the new latch state on every transition (rising
            # edge → latched=True, falling edge → latched=False, no
            # transition → no write needed)
            if new_latched != prev_latched:
                self.sm.update(over_cap_latched=new_latched)
        except Exception as e:
            self.log.warning(f"on-chain reconcile failed: {type(e).__name__}: {e}")
        decision = self.ks.check()
        if not decision.allowed:
            self.log.info(f"Skipping tick — {decision.reason}")
            return

        candidates = await self._fetch_candidates()
        self.log.info(f"Fetched {len(candidates)} sports markets")

        # Markets the user already has an open position in (on-chain).
        # During smoke test we additionally reject these — the smoke test
        # is "1 position per market", not "1 trade per day". Prevents the
        # 5x duplication incident from 2026-06-09.
        held_markets = set()
        s = self.sm.read()
        onchain_by_market = s.get("onchain_positions_by_market", {}) or {}
        for cid, by_outcome in onchain_by_market.items():
            total = sum(float(v) for v in by_outcome.values() if v)
            if total > 0:
                held_markets.add(cid)

        placed = 0
        for market in candidates:
            off = _is_offtopic(market)
            if off:
                self.log.debug(f"Reject off-topic: {market.get('question','?')[:50]} — {off}")
                continue
            if market.get("condition_id") in held_markets:
                self.log.debug(
                    f"Reject smoke-test: already hold "
                    f"{market.get('condition_id', '?')[:12]}… on-chain"
                )
                continue
            sig = evaluate_market(market, config=self.cfg)
            if not sig.accepted:
                self.log.debug(f"Reject signal: {market.get('question','?')[:50]} — {sig.reason}")
                continue
            size = min(sig.size_usd, self.cfg.per_trade_cap_usd)
            # Pre-trade cap check (added 2026-06-10): refuse to place an
            # order that would push on-chain exposure over the cap. The
            # kill switch also blocks on overage, but only as a circuit
            # breaker AFTER the fact — this prevents the thrashing where
            # every tick re-crosses the line and floods Telegram with
            # "exposure $X exceeds cap" alerts. Get fresh exposure from
            # the data-api right before the trade so the decision uses
            # ground truth, not the last reconcile (which may be up to
            # 5 min stale).
            from bot import onchain_reconcile as _oc_for_check
            fresh = _oc_for_check.fetch_positions(self.cfg.proxy_address, min_value=0.0)
            current = sum(p.get("current_value", 0) for p in fresh)
            if current + size > self.cfg.total_open_exposure_usd:
                self.log.info(
                    f"Pre-trade cap: would push exposure "
                    f"${current + size:.2f} > cap ${self.cfg.total_open_exposure_usd:.2f} "
                    f"(this trade ${size:.2f} skipped)"
                )
                # Edge-triggered Telegram alert (2026-06-12): only fire on
                # the rising edge of the over-cap block. The previous
                # level-triggered `cap_alert_sent_tick` flag reset every
                # tick, so Telegram got pinged every 5 min while the
                # exposure stayed over cap. Now we latch on the rising
                # edge and only re-arm when exposure drops back below
                # the cap (handled in the success branch below).
                # The kill-switch halt message already covers the
                # "we're over cap" case for the on-chain reconcile path.
                if placed == 0:
                    prev_latched = bool(
                        self.sm.read().get("pre_trade_over_cap_latched", False)
                    )
                    # The pre-trade alert fires when a trade WOULD push
                    # us over the cap, even if current exposure itself
                    # is under the cap. The "edge" is: first time this
                    # blocking condition is hit since we last had
                    # headroom (latch reset by a successful trade or
                    # by no-candidates path). If we're already latched
                    # from a previous tick, stay silent.
                    if not prev_latched:
                        self.notifier.error(
                            f"Pre-trade cap: skipping ${size:.2f} trade — "
                            f"on-chain ${current:.2f} + ${size:.2f} would exceed "
                            f"cap ${self.cfg.total_open_exposure_usd:.2f}. "
                            f"Bot resumes once exposure is reduced."
                        )
                        self.sm.update(pre_trade_over_cap_latched=True)
                continue
            order_id = await self._place_order(
                market, size_usd=size, price=market["best_ask"]
            )
            if order_id:
                # A trade was placed: re-arm the pre-trade over-cap
                # latch. If subsequent ticks hit the cap again, the
                # alert will fire once on the new rising edge.
                if bool(self.sm.read().get("pre_trade_over_cap_latched", False)):
                    self.sm.update(pre_trade_over_cap_latched=False)
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
                # Record cost basis for smart-exit (directional = sports-market path)
                try:
                    from bot import smart_exit as _se_for_trade
                    _se_for_trade.ensure_positions_cost_table(self.cfg.state_dir)
                    _se_for_trade.record_buy(
                        state_dir=self.cfg.state_dir,
                        token_id=market.get("token_id", ""),
                        buy_price=market["best_ask"],
                        size_shares=size / market["best_ask"] if market["best_ask"] > 0 else 0.0,
                        kind="directional",
                    )
                except Exception as rec_err:
                    self.log.warning(f"positions_cost record_buy err: {rec_err}")
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
