from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from core.timeutils import seconds_until_next_utc_day, utc_day_key, utc_now
from core.types import AdaptationMode, Side
from services.positions import exposure_of

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.running = True
        self.last_fill_poll = None
        self.last_hour = None
        self.processed_fill_ids: set[str] = set()

    async def run(self) -> None:
        await self._load_mode_state()
        await self._load_mispricing_state()
        await self.ctx.notifier.send(f"bot start mode={self.ctx.settings.env.mode.value}")
        await self._select_markets()
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                log.exception("loop error: %s", e)
                if self.ctx.settings.env.cancel_all_on_exit:
                    await self.ctx.exec.cancel_all()
            await asyncio.sleep(self.ctx.settings.env.refresh_sec)

    async def _load_mode_state(self) -> None:
        raw = await self.ctx.repo.get_bot_state("adaptation_mode")
        if not raw:
            return
        payload = json.loads(raw)
        expires_at = payload.get("expires_at")
        if not expires_at:
            return
        exp_ts = datetime.fromisoformat(expires_at)
        if utc_now() < exp_ts:
            self.ctx.state.stats.mode = AdaptationMode(payload.get("mode", AdaptationMode.NORMAL.value))
        else:
            await self.ctx.repo.set_bot_state("adaptation_mode", "")

    async def _load_mispricing_state(self) -> None:
        raw = await self.ctx.repo.get_bot_state("mispricing_active_trades")
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("invalid bot_state payload for mispricing_active_trades; ignoring")
            return
        if isinstance(payload, list):
            self.ctx.mis.restore_active_trades(payload)
            if self.ctx.mis.active_trades:
                log.info("restored active mispricing trades count=%d", len(self.ctx.mis.active_trades))

    async def _persist_mispricing_state(self) -> None:
        await self.ctx.repo.set_bot_state("mispricing_active_trades", json.dumps(self.ctx.mis.serialize_active_trades()))

    async def _persist_mode_state(self, mode: AdaptationMode, hours: int) -> None:
        now = utc_now()
        payload = {
            "mode": mode.value,
            "activated_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=hours)).isoformat(),
        }
        await self.ctx.repo.set_bot_state("adaptation_mode", json.dumps(payload))

    @staticmethod
    def _normalize_event_url(raw: str) -> str:
        cleaned = raw.strip().lower().rstrip("/")
        if not cleaned:
            return cleaned
        if "/event/" in cleaned:
            return cleaned.split("/event/")[-1]
        return cleaned

    async def _select_markets(self):
        markets = await self.ctx.exchange.fetch_markets()
        available = {self._normalize_event_url(m.event_url): m for m in markets}
        configured = self.ctx.settings.markets.markets
        if len(configured) > self.ctx.settings.risk.max_open_markets:
            raise ValueError("configured markets exceed risk.max_open_markets; refuse to auto-substitute")
        prob_min = self.ctx.settings.markets.selection["prob_min"]
        prob_max = self.ctx.settings.markets.selection["prob_max"]

        log.info("market resolution: configured=%d exchange_available=%d", len(configured), len(available))
        for cfg in configured:
            target_url = cfg["event_url"]
            market = available.get(self._normalize_event_url(target_url))
            if market is None:
                raise ValueError(f"configured market URL not returned by exchange: {target_url}")
            out = self.ctx.selector(market, prob_min, prob_max)
            self.ctx.state.selected_outcomes[market.market_id] = out.outcome_id
            await self.ctx.repo.save_selected_market(market.market_id, market.name, out.outcome_id, out.label, out.implied_prob, out.volume, str(utc_now()))
            reason = f"within prob_band=[{prob_min:.2f},{prob_max:.2f}] max_liquidity={out.volume:.0f}"
            log.info("configured market matched target_url=%s market_id=%s", target_url, market.market_id)
            log.info("selected outcome market=%s outcome=%s(%s) prob=%.4f volume=%.1f reason=%s", market.market_id, out.outcome_id, out.label, out.implied_prob, out.volume, reason)
            await self.ctx.notifier.send(f"selected outcome {market.name}: {out.label} p={out.implied_prob:.2f} liq={out.volume:.0f}; reason={reason}")

    async def _tick(self):
        now = utc_now()
        self.ctx.pnl_engine.maybe_reset_month(now, self.ctx.pnl_state["equity"])
        if self.ctx.state.stats.day_key != utc_day_key(now):
            await self._on_new_day()
        if self.ctx.state.is_paused():
            log.info("risk pause active until=%s", self.ctx.state.pause_until.isoformat() if self.ctx.state.pause_until else "unknown")
            return

        mids: dict[tuple[str, str], float] = {}
        positions = await self.ctx.exchange.fetch_positions()
        self.ctx.state.positions = {(p.market_id, p.outcome_id): p for p in positions}
        await self.ctx.exec.reconcile_open_orders()
        open_orders = await self.ctx.exchange.fetch_open_orders()

        for market_id, outcome_id in self.ctx.state.selected_outcomes.items():
            res = await self.ctx.exchange.get_market_resolution_time(market_id)
            if self.ctx.risk_engine.near_resolution(res, self.ctx.settings.risk.pause_before_resolution_minutes):
                reason = f"near_resolution({res.isoformat() if res else 'unknown'})"
                if self.ctx.state.blocked_markets.get(market_id) != reason:
                    self.ctx.state.blocked_markets[market_id] = reason
                    await self.ctx.exec.cancel_market_orders(market_id, outcome_id, reason=reason)
                    await self.ctx.notifier.send(f"market {market_id} blocked: {reason}")
                    log.warning("near-resolution block market=%s outcome=%s resolution=%s", market_id, outcome_id, res.isoformat() if res else "unknown")
                continue
            self.ctx.state.blocked_markets.pop(market_id, None)

            book = await self.ctx.exchange.fetch_orderbook(market_id, outcome_id)
            mids[(market_id, outcome_id)] = book.mid
            p = self.ctx.state.positions.get((market_id, outcome_id))
            exp = exposure_of(p) if p else 0.0
            mode_mult_mm, mode_mult_mis = self.ctx.mode_multipliers()
            sizing = self.ctx.risk_engine.dynamic_sizing(self.ctx.pnl_state["equity"], mode_mult_mm, mode_mult_mis)

            self.ctx.mis.on_tick(market_id, outcome_id, book.mid)
            for action in self.ctx.mis.manage_trade(market_id, outcome_id, book.mid):
                px = book.best_bid if action.side == Side.SELL else book.best_ask
                exit_order = await self.ctx.exec.place_limit(market_id, outcome_id, action.side, px, action.size, tag="mis_exit")
                self.ctx.mis.register_exit_order(exit_order.order_id, market_id, outcome_id, action.side, action.reason, action.size)
                await self.ctx.notifier.send(f"mispricing exit trigger={action.reason} market={market_id} outcome={outcome_id} size={action.size}")
                log.info("mispricing exit triggered market=%s outcome=%s reason=%s size=%.4f", market_id, outcome_id, action.reason, action.size)

            has_mis_context = (
                self.ctx.mis.has_active_trade(market_id, outcome_id)
                or self.ctx.mis.has_pending_entry(market_id, outcome_id)
                or self.ctx.mis.has_pending_exit(market_id, outcome_id)
            )

            is_blocked = market_id in self.ctx.state.blocked_markets
            if not is_blocked:
                if has_mis_context:
                    await self.ctx.exec.cancel_market_orders(market_id, outcome_id, reason="mispricing_active", tag_filter={"mm_quote"})
                    log.info("MM paused due to active/pending mispricing trade market=%s outcome=%s", market_id, outcome_id)
                else:
                    (bside, bid), (sside, ask), reduce_only = self.ctx.mm.build_quotes(book, exp, sizing.max_exposure_per_outcome)
                    for o in open_orders:
                        if o.market_id == market_id and o.outcome_id == outcome_id and self.ctx.exec.order_tags.get(o.order_id) == "mm_quote":
                            await self.ctx.exec.cancel(o.order_id, reason="mm_refresh")
                    if not reduce_only and exp < sizing.max_exposure_per_outcome:
                        await self.ctx.exec.place_limit(market_id, outcome_id, bside, bid, sizing.order_size_mm, tag="mm_quote")
                    if (not reduce_only and exp < sizing.max_exposure_per_outcome) or (p and p.qty > 0):
                        await self.ctx.exec.place_limit(market_id, outcome_id, sside, ask, sizing.order_size_mm, tag="mm_quote")

                sig = self.ctx.mis.detect_signal(market_id, outcome_id)
                if sig:
                    if has_mis_context:
                        log.info("mispricing signal rejected market=%s outcome=%s reason=existing_trade_or_orders", market_id, outcome_id)
                    elif self.ctx.state.stats.mispricing_trades_today >= self.ctx.settings.risk.max_mispricing_trades_per_day:
                        log.info("mispricing signal rejected market=%s outcome=%s reason=max_trades_reached", market_id, outcome_id)
                    elif exp >= sizing.max_exposure_per_outcome:
                        log.info("mispricing signal rejected market=%s outcome=%s reason=max_exposure reached=%.4f max=%.4f", market_id, outcome_id, exp, sizing.max_exposure_per_outcome)
                    else:
                        px = book.best_ask if sig == Side.BUY else book.best_bid
                        entry_order = await self.ctx.exec.place_limit(market_id, outcome_id, sig, px, sizing.order_size_mis, tag="mis_entry")
                        self.ctx.mis.register_entry_order(entry_order.order_id, market_id, outcome_id, sig)
                        log.info("mispricing signal accepted market=%s outcome=%s side=%s price=%.4f size=%.4f", market_id, outcome_id, sig.value, px, sizing.order_size_mis)

        fills = await self.ctx.exchange.fetch_fills(self.last_fill_poll)
        self.last_fill_poll = now
        for f in fills:
            if f.fill_id in self.processed_fill_ids:
                continue
            self.processed_fill_ids.add(f.fill_id)
            await self.ctx.repo.insert_fill(f)
            order_state = await self.ctx.repo.apply_fill_to_order(f.order_id, f.size, str(f.ts))
            if order_state:
                log.info("order fill reconciled order_id=%s filled=%.4f/%.4f status=%s", f.order_id, order_state[1], order_state[0], order_state[2])
            self.ctx.state.stats.trades_today += 1
            mis_state = self.ctx.mis.apply_fill(f)
            if mis_state:
                if bool(mis_state.get("opened")):
                    self.ctx.state.stats.mispricing_trades_today += 1
                    await self.ctx.notifier.send(
                        f"mispricing trade opened market={f.market_id} outcome={f.outcome_id} side={f.side.value} entry={float(mis_state.get('entry_price', 0.0)):.4f}"
                    )
                if bool(mis_state.get("closed")):
                    close_reason = "stop" if bool(mis_state.get("stop_hit")) else "time_stop" if bool(mis_state.get("time_stop_hit")) else "tp"
                    await self.ctx.notifier.send(f"mispricing trade closed market={f.market_id} outcome={f.outcome_id} reason={close_reason}")
                    log.info("mispricing trade closed market=%s outcome=%s reason=%s", f.market_id, f.outcome_id, close_reason)
                if bool(mis_state.get("stop_hit")):
                    self.ctx.state.stats.stopouts_today += 1
                    log.warning("mispricing stopout market=%s outcome=%s", f.market_id, f.outcome_id)
            p = self.ctx.state.positions.get((f.market_id, f.outcome_id))
            if p:
                log.info("position update market=%s outcome=%s qty=%.4f avg=%.4f rpnl=%.4f upnl=%.4f", p.market_id, p.outcome_id, p.qty, p.avg_price, p.realized_pnl, p.unrealized_pnl)
            await self.ctx.notifier.send(f"fill {f.fill_id} {f.side.value} {f.size}@{f.price}")

        cash = await self.ctx.exchange.fetch_balance()
        equity, drawdown = self.ctx.pnl_engine.mark_to_market(cash, list(self.ctx.state.positions.values()), mids)
        pnl_today = self.ctx.pnl_engine.pnl_today(equity)
        pnl_mtd = self.ctx.pnl_engine.pnl_mtd(equity)
        progress = self.ctx.pnl_engine.progress_to_goal_500(equity)
        self.ctx.pnl_state = {"equity": equity, "pnl_today": pnl_today, "pnl_mtd": pnl_mtd, "progress": progress, "drawdown": drawdown}
        log.info("pnl update equity=%.4f pnl_today=%.4f pnl_mtd=%.4f drawdown=%.4f", equity, pnl_today, pnl_mtd, drawdown)

        if self.ctx.risk_engine.should_kill_switch(pnl_today, equity, self.ctx.state):
            self.ctx.risk_engine.activate_pause_to_next_day(self.ctx.state)
            await self.ctx.exec.cancel_all()
            await self.ctx.notifier.send("kill switch activated; pause until next UTC day")
            log.warning("kill switch activated pnl_today=%.4f equity=%.4f", pnl_today, equity)

        await self.ctx.repo.snapshot_pnl(str(now), equity, pnl_today, pnl_mtd, progress, self.ctx.state.stats.mode.value, drawdown)
        for p in self.ctx.state.positions.values():
            await self.ctx.repo.snapshot_position(str(now), p, exposure_of(p))
        await self._persist_mispricing_state()

        if self.last_hour != now.hour:
            self.last_hour = now.hour
            await self.ctx.reporter.hourly(
                {
                    "equity_now": equity,
                    "pnl_today": pnl_today,
                    "pnl_mtd": pnl_mtd,
                    "progress_to_goal_500": progress,
                    "open_markets": len(self.ctx.state.selected_outcomes),
                    "stopouts_today": self.ctx.state.stats.stopouts_today,
                    "trades_today": self.ctx.state.stats.trades_today,
                    "mispricing_trades_today": self.ctx.state.stats.mispricing_trades_today,
                    "mode": self.ctx.state.stats.mode.value,
                    "positions": ", ".join([f"{k}:{v.qty:.2f}" for k, v in self.ctx.state.positions.items()]),
                }
            )

    async def _on_new_day(self):
        old_day = self.ctx.state.stats.day_key
        prev_equity = self.ctx.pnl_state["equity"]
        pnl_day = self.ctx.pnl_state["pnl_today"]
        await self.ctx.repo.upsert_daily_metrics(old_day, self.ctx.state.stats.trades_today, self.ctx.state.stats.stopouts_today, self.ctx.state.stats.mispricing_trades_today, pnl_day)
        await self.ctx.reporter.daily(
            {
                "equity_start": self.ctx.pnl_engine.equity_start_day,
                "equity_end": prev_equity,
                "pnl_day": pnl_day,
                "pnl_mtd": self.ctx.pnl_state["pnl_mtd"],
                "progress": self.ctx.pnl_state["progress"],
                "stopouts": self.ctx.state.stats.stopouts_today,
                "trades": self.ctx.state.stats.trades_today,
                "mis_trades": self.ctx.state.stats.mispricing_trades_today,
                "max_drawdown": self.ctx.pnl_state["drawdown"],
                "mode": self.ctx.state.stats.mode.value,
            }
        )
        last3 = await self.ctx.repo.get_last_n_daily_pnl(3)
        mode = AdaptationMode.NORMAL
        if len(last3) >= 3 and sum(last3) > self.ctx.settings.risk.accel_3d_pnl_threshold_pct * prev_equity and self.ctx.state.stats.stopouts_today <= 1:
            mode = AdaptationMode.ACCEL
        if self.ctx.state.stats.stopouts_today > 0 or (len(last3) >= 2 and last3[0] < 0 and last3[1] < 0):
            mode = AdaptationMode.BRAKE
        old_mode = self.ctx.state.stats.mode
        self.ctx.state.reset_daily()
        self.ctx.state.stats.mode = mode
        log.info("mode transition old=%s new=%s", old_mode.value, mode.value)
        await self._persist_mode_state(mode, self.ctx.settings.risk.adaptation_window_hours)
        self.ctx.pnl_engine.reset_day(prev_equity)
        await self.ctx.notifier.send(f"new UTC day, mode={mode.value}, next reset in {seconds_until_next_utc_day()} sec")

    async def shutdown(self):
        self.running = False
        if self.ctx.settings.env.cancel_all_on_exit and self.ctx.settings.env.mode.value == "LIVE":
            await self.ctx.exec.cancel_all()
        await self.ctx.notifier.send("bot stop")
