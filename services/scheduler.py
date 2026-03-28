from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

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

    async def run(self) -> None:
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

    async def _select_markets(self):
        markets = await self.ctx.exchange.fetch_markets()
        prob_min = self.ctx.settings.markets.selection["prob_min"]
        prob_max = self.ctx.settings.markets.selection["prob_max"]
        for m in markets[: self.ctx.settings.risk.max_open_markets]:
            out = self.ctx.selector(m, prob_min, prob_max)
            self.ctx.state.selected_outcomes[m.market_id] = out.outcome_id
            await self.ctx.repo.save_selected_market(m.market_id, m.name, out.outcome_id, out.label, out.implied_prob, out.volume, str(utc_now()))
            await self.ctx.notifier.send(f"selected outcome {m.name}: {out.label} p={out.implied_prob:.2f} liq={out.volume:.0f}")

    async def _tick(self):
        now = utc_now()
        if self.ctx.state.stats.day_key != utc_day_key(now):
            await self._on_new_day()
        if self.ctx.state.is_paused():
            return

        mids: dict[tuple[str, str], float] = {}
        positions = await self.ctx.exchange.fetch_positions()
        for p in positions:
            self.ctx.state.positions[(p.market_id, p.outcome_id)] = p

        for market_id, outcome_id in self.ctx.state.selected_outcomes.items():
            res = await self.ctx.exchange.get_market_resolution_time(market_id)
            if self.ctx.risk_engine.near_resolution(res, self.ctx.settings.risk.pause_before_resolution_minutes):
                await self.ctx.exec.cancel_all()
                continue
            book = await self.ctx.exchange.fetch_orderbook(market_id, outcome_id)
            mids[(market_id, outcome_id)] = book.mid
            p = self.ctx.state.positions.get((market_id, outcome_id))
            exp = exposure_of(p) if p else 0.0
            mode_mult_mm, mode_mult_mis = self.ctx.mode_multipliers()
            sizing = self.ctx.risk_engine.dynamic_sizing(self.ctx.pnl_state["equity"], mode_mult_mm, mode_mult_mis)
            (bside, bid), (sside, ask), reduce_only = self.ctx.mm.build_quotes(book, exp, sizing.max_exposure_per_outcome)
            open_orders = await self.ctx.exchange.fetch_open_orders()
            for o in open_orders:
                if o.market_id == market_id and o.outcome_id == outcome_id:
                    await self.ctx.exec.cancel(o.order_id)
            if not reduce_only and exp < sizing.max_exposure_per_outcome:
                await self.ctx.exec.place_limit(market_id, outcome_id, bside, bid, sizing.order_size_mm)
            if (not reduce_only and exp < sizing.max_exposure_per_outcome) or (p and p.qty > 0):
                await self.ctx.exec.place_limit(market_id, outcome_id, sside, ask, sizing.order_size_mm)

            self.ctx.mis.on_tick(market_id, outcome_id, book.mid)
            sig = self.ctx.mis.detect_signal(market_id, outcome_id)
            if sig and self.ctx.state.stats.mispricing_trades_today < self.ctx.settings.risk.max_mispricing_trades_per_day:
                if exp < sizing.max_exposure_per_outcome:
                    side = sig
                    px = book.best_ask if side == Side.BUY else book.best_bid
                    await self.ctx.exec.place_limit(market_id, outcome_id, side, px, sizing.order_size_mis)
                    self.ctx.state.stats.mispricing_trades_today += 1

        fills = await self.ctx.exchange.fetch_fills(self.last_fill_poll)
        self.last_fill_poll = now
        for f in fills:
            await self.ctx.repo.insert_fill(f)
            self.ctx.state.stats.trades_today += 1
            await self.ctx.notifier.send(f"fill {f.fill_id} {f.side.value} {f.size}@{f.price}")

        cash = await self.ctx.exchange.fetch_balance()
        equity, drawdown = self.ctx.pnl_engine.mark_to_market(cash, list(self.ctx.state.positions.values()), mids)
        pnl_today = self.ctx.pnl_engine.pnl_today(equity)
        pnl_mtd = self.ctx.pnl_engine.pnl_mtd(equity)
        progress = self.ctx.pnl_engine.progress_to_goal_500(equity)
        self.ctx.pnl_state = {"equity": equity, "pnl_today": pnl_today, "pnl_mtd": pnl_mtd, "progress": progress, "drawdown": drawdown}

        if self.ctx.risk_engine.should_kill_switch(pnl_today, equity, self.ctx.state):
            self.ctx.state.stats.stopouts_today += 1
            self.ctx.risk_engine.activate_pause_to_next_day(self.ctx.state)
            await self.ctx.exec.cancel_all()
            await self.ctx.notifier.send("kill switch activated; pause until next UTC day")

        await self.ctx.repo.snapshot_pnl(str(now), equity, pnl_today, pnl_mtd, progress, self.ctx.state.stats.mode.value, drawdown)
        for p in self.ctx.state.positions.values():
            await self.ctx.repo.snapshot_position(str(now), p, exposure_of(p))

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
        self.ctx.state.reset_daily()
        self.ctx.state.stats.mode = mode
        self.ctx.pnl_engine.reset_day(prev_equity)
        await self.ctx.notifier.send(f"new UTC day, mode={mode.value}, next reset in {seconds_until_next_utc_day()} sec")

    async def shutdown(self):
        self.running = False
        if self.ctx.settings.env.cancel_all_on_exit and self.ctx.settings.env.mode.value == "LIVE":
            await self.ctx.exec.cancel_all()
        await self.ctx.notifier.send("bot stop")
