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
        self.last_position_snapshot_sig: dict[tuple[str, str], tuple[float, float, float, float, float]] = {}
        self._last_mis_mm_pause_log_ts: dict[tuple[str, str], datetime] = {}
        self._last_pause_log_ts: datetime | None = None

    async def run(self) -> None:
        await self._load_mode_state()
        await self._load_mispricing_state()
        await self.ctx.notifier.send(
            f"bot start mode={self.ctx.settings.env.mode.value}",
            dedupe_key=f"startup:{self.ctx.settings.env.mode.value}:{self.ctx.settings.env.db_path}",
            dedupe_ttl_sec=600,
        )
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
            await self.ctx.notifier.send(
                f"selected outcome {market.name}: {out.label} p={out.implied_prob:.2f} liq={out.volume:.0f}; reason={reason}",
                dedupe_key=f"selected_outcome:{market.market_id}:{out.outcome_id}:{self.ctx.state.stats.day_key}",
                dedupe_ttl_sec=86400,
            )

    def _prune_stale_mispricing_trades(self) -> None:
        # Recovery hardening: if an "active" mispricing trade has no actual position exposure,
        # treat it as stale state and drop it to avoid resurrection after restart/crash windows.
        stale_keys: list[tuple[str, str]] = []
        for key, trade in self.ctx.mis.active_trades.items():
            if trade.closed:
                stale_keys.append(key)
                continue
            pos = self.ctx.state.positions.get(key)
            qty = abs(float(pos.qty)) if pos else 0.0
            if qty <= 1e-6 and trade.remaining_size > 0:
                stale_keys.append(key)
        for key in stale_keys:
            self.ctx.mis.active_trades.pop(key, None)
            self.ctx.mis.pending_exit_orders.pop(key, None)
            log.warning("pruned stale mispricing trade with no position exposure market=%s outcome=%s", key[0], key[1])

    async def _tick(self):
        now = utc_now()
        self.ctx.pnl_engine.maybe_reset_month(now, self.ctx.pnl_state["equity"])
        if self.ctx.state.stats.day_key != utc_day_key(now):
            await self._on_new_day()
        if self.ctx.state.is_paused():
            if self._last_pause_log_ts is None or (now - self._last_pause_log_ts).total_seconds() >= 300:
                until = self.ctx.state.pause_until.isoformat() if self.ctx.state.pause_until else "unknown"
                log.info(
                    "risk pause active until=%s kill_switch_active=%s reason=%s",
                    until,
                    self.ctx.state.kill_switch_active,
                    self.ctx.state.pause_reason or "unspecified",
                )
                self._last_pause_log_ts = now
            return
        self._last_pause_log_ts = None

        mids: dict[tuple[str, str], float] = {}
        await self._refresh_positions(reason="tick_start")
        self._prune_stale_mispricing_trades()
        await self.ctx.exec.reconcile_open_orders()
        open_orders = await self.ctx.exchange.fetch_open_orders()

        for market_id, outcome_id in self.ctx.state.selected_outcomes.items():
            res = await self.ctx.exchange.get_market_resolution_time(market_id)
            if self.ctx.risk_engine.near_resolution(res, self.ctx.settings.risk.pause_before_resolution_minutes):
                reason = f"near_resolution({res.isoformat() if res else 'unknown'})"
                if self.ctx.state.blocked_markets.get(market_id) != reason:
                    self.ctx.state.blocked_markets[market_id] = reason
                    await self.ctx.exec.cancel_market_orders(market_id, outcome_id, reason=reason)
                    await self.ctx.notifier.send(
                        f"market {market_id} blocked: {reason}",
                        dedupe_key=f"near_resolution_block:{market_id}:{reason}",
                        dedupe_ttl_sec=3600,
                    )
                    log.warning("near-resolution block market=%s outcome=%s resolution=%s", market_id, outcome_id, res.isoformat() if res else "unknown")
                continue
            if market_id in self.ctx.state.blocked_markets:
                old_reason = self.ctx.state.blocked_markets.pop(market_id)
                log.info("market unblocked market=%s reason=%s", market_id, old_reason)

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
                await self.ctx.notifier.send(
                    f"mispricing exit trigger={action.reason} market={market_id} outcome={outcome_id} size={action.size}",
                    dedupe_key=f"mis_exit_trigger:{market_id}:{outcome_id}:{action.reason}",
                    dedupe_ttl_sec=max(30, self.ctx.settings.env.refresh_sec * 4),
                )
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
                    last = self._last_mis_mm_pause_log_ts.get((market_id, outcome_id))
                    if last is None or (now - last).total_seconds() >= 120:
                        log.info(
                            "MM paused for safety due to active/pending mispricing context market=%s outcome=%s rule=no_inventory_building_on_same_outcome",
                            market_id,
                            outcome_id,
                        )
                        await self.ctx.notifier.send(
                            f"MM paused for market={market_id} outcome={outcome_id}: active mispricing context; contradictory inventory quotes suppressed",
                            dedupe_key=f"mm_pause:{market_id}:{outcome_id}",
                            dedupe_ttl_sec=300,
                        )
                        self._last_mis_mm_pause_log_ts[(market_id, outcome_id)] = now
                else:
                    self._last_mis_mm_pause_log_ts.pop((market_id, outcome_id), None)
                    (bside, bid), (sside, ask), reduce_only = self.ctx.mm.build_quotes(book, exp, sizing.max_exposure_per_outcome)
                    for o in open_orders:
                        if o.market_id == market_id and o.outcome_id == outcome_id and self.ctx.exec.order_tags.get(o.order_id) == "mm_quote":
                            await self.ctx.exec.cancel(o.order_id, reason="mm_refresh")
                    if not reduce_only and exp < sizing.max_exposure_per_outcome:
                        await self.ctx.exec.place_limit(market_id, outcome_id, bside, bid, sizing.order_size_mm, tag="mm_quote")
                    available_qty = max(0.0, float(p.qty)) if p else 0.0
                    if available_qty > 0:
                        sell_size = min(sizing.order_size_mm, available_qty)
                        await self.ctx.exec.place_limit(market_id, outcome_id, sside, ask, sell_size, tag="mm_quote")

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

        fills_seen = 0
        fills = await self.ctx.exchange.fetch_fills(self.last_fill_poll)
        self.last_fill_poll = now
        for f in fills:
            if f.fill_id in self.processed_fill_ids:
                continue
            inserted = await self.ctx.repo.insert_fill(f)
            if not inserted:
                log.info("duplicate fill ignored fill_id=%s order_id=%s", f.fill_id, f.order_id)
                self.processed_fill_ids.add(f.fill_id)
                continue
            fills_seen += 1
            self.processed_fill_ids.add(f.fill_id)
            order_state = await self.ctx.repo.apply_fill_to_order(f.order_id, f.size, str(f.ts))
            if order_state:
                log.info("order fill reconciled order_id=%s filled=%.4f/%.4f status=%s", f.order_id, order_state[1], order_state[0], order_state[2])
            else:
                log.warning("fill received for unknown order_id=%s fill_id=%s", f.order_id, f.fill_id)
            self.ctx.state.stats.trades_today += 1
            mis_state = self.ctx.mis.apply_fill(f)
            if mis_state:
                if bool(mis_state.get("opened")):
                    self.ctx.state.stats.mispricing_trades_today += 1
                    await self.ctx.notifier.send(
                        f"mispricing trade opened market={f.market_id} outcome={f.outcome_id} side={f.side.value} entry={float(mis_state.get('entry_price', 0.0)):.4f}",
                        dedupe_key=f"mis_open:{f.market_id}:{f.outcome_id}:{self.ctx.state.stats.day_key}",
                        dedupe_ttl_sec=1800,
                    )
                exit_event = str(mis_state.get("exit_event") or "")
                if exit_event:
                    await self.ctx.notifier.send(
                        f"mispricing exit filled market={f.market_id} outcome={f.outcome_id} reason={exit_event} remaining={float(mis_state.get('remaining_size', 0.0)):.4f}",
                        dedupe_key=f"mis_exit_fill:{f.market_id}:{f.outcome_id}:{exit_event}",
                        dedupe_ttl_sec=300,
                    )
                if bool(mis_state.get("closed")):
                    close_reason = "stop" if bool(mis_state.get("stop_hit")) else "time_stop" if bool(mis_state.get("time_stop_hit")) else "tp"
                    await self.ctx.notifier.send(
                        f"mispricing trade closed market={f.market_id} outcome={f.outcome_id} reason={close_reason}",
                        dedupe_key=f"mis_closed:{f.market_id}:{f.outcome_id}:{close_reason}:{self.ctx.state.stats.day_key}",
                        dedupe_ttl_sec=86400,
                    )
                    log.info("mispricing trade closed market=%s outcome=%s reason=%s", f.market_id, f.outcome_id, close_reason)
                if bool(mis_state.get("stopout_increment")):
                    self.ctx.state.stats.stopouts_today += 1
                    log.warning("mispricing stopout market=%s outcome=%s stopouts_today=%d", f.market_id, f.outcome_id, self.ctx.state.stats.stopouts_today)
            await self.ctx.notifier.send(f"fill {f.fill_id} {f.side.value} {f.size}@{f.price}")

        if fills_seen > 0:
            await self._refresh_positions(reason="post_fill")

        cash = await self.ctx.exchange.fetch_balance()
        equity, drawdown = self.ctx.pnl_engine.mark_to_market(cash, list(self.ctx.state.positions.values()), mids)
        pnl_today = self.ctx.pnl_engine.pnl_today(equity)
        pnl_mtd = self.ctx.pnl_engine.pnl_mtd(equity)
        progress = self.ctx.pnl_engine.progress_to_goal_500(equity)
        self.ctx.pnl_state = {"equity": equity, "pnl_today": pnl_today, "pnl_mtd": pnl_mtd, "progress": progress, "drawdown": drawdown}
        log.info(
            "pnl update cash=%.4f equity=%.4f pnl_today=%.4f pnl_mtd=%.4f drawdown=%.4f positions=%d",
            cash,
            equity,
            pnl_today,
            pnl_mtd,
            drawdown,
            len(self.ctx.state.positions),
        )

        kill_reason = self.ctx.risk_engine.kill_switch_reason(pnl_today, equity, self.ctx.state)
        if kill_reason:
            self.ctx.risk_engine.activate_pause_to_next_day(self.ctx.state, reason=kill_reason)
            await self.ctx.exec.cancel_all()
            pause_until = self.ctx.state.pause_until.isoformat() if self.ctx.state.pause_until else "unknown"
            await self.ctx.notifier.send(
                f"kill switch activated reason={kill_reason}; pause until next UTC day ({pause_until})",
                dedupe_key=f"kill_switch:{self.ctx.state.stats.day_key}",
                dedupe_ttl_sec=3600,
            )
            await self.ctx.notifier.send(
                f"trading paused until next UTC day ({pause_until}) reason={kill_reason}",
                dedupe_key=f"pause_until_next_day:{self.ctx.state.stats.day_key}",
                dedupe_ttl_sec=3600,
            )
            log.warning("kill switch activated reason=%s pnl_today=%.4f equity=%.4f pause_until=%s", kill_reason, pnl_today, equity, pause_until)

        await self._snapshot_positions(now)
        await self.ctx.repo.snapshot_pnl(str(now), equity, pnl_today, pnl_mtd, progress, self.ctx.state.stats.mode.value, drawdown)
        log.info(
            "pnl snapshot written ts=%s equity=%.4f pnl_today=%.4f pnl_mtd=%.4f drawdown=%.4f",
            now.isoformat(),
            equity,
            pnl_today,
            pnl_mtd,
            drawdown,
        )
        await self._persist_mispricing_state()

        if self.last_hour != now.hour:
            self.last_hour = now.hour
            await self.ctx.reporter.hourly(
                {
                    "ts": now.isoformat(),
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
                "day_key": old_day,
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
        if old_mode != mode:
            await self.ctx.notifier.send(
                f"adaptation mode changed {old_mode.value} -> {mode.value}",
                dedupe_key=f"mode_change:{old_day}:{old_mode.value}:{mode.value}",
                dedupe_ttl_sec=86400,
            )
        await self._persist_mode_state(mode, self.ctx.settings.risk.adaptation_window_hours)
        self.ctx.pnl_engine.reset_day(prev_equity)
        await self.ctx.notifier.send(
            f"new UTC day, mode={mode.value}, next reset in {seconds_until_next_utc_day()} sec",
            dedupe_key=f"utc_day_start:{self.ctx.state.stats.day_key}",
            dedupe_ttl_sec=3600,
        )

    async def _snapshot_positions(self, ts: datetime) -> None:
        for p in self.ctx.state.positions.values():
            sig = (
                round(float(p.qty), 8),
                round(float(p.avg_price), 8),
                round(float(exposure_of(p)), 8),
                round(float(p.realized_pnl), 8),
                round(float(p.unrealized_pnl), 8),
            )
            prev_sig = self.last_position_snapshot_sig.get((p.market_id, p.outcome_id))
            drift_vals: list[str] = []
            if sig[2] != round(abs(sig[0] * sig[1]), 8):
                drift_vals.append("exposure")
            if prev_sig and self._has_unexpected_position_drift(prev_sig, sig):
                log.warning(
                    "position snapshot drift detected market=%s outcome=%s prev=%s now=%s fields=%s",
                    p.market_id,
                    p.outcome_id,
                    prev_sig,
                    sig,
                    ",".join(drift_vals) if drift_vals else "unknown",
                )
            await self.ctx.repo.snapshot_position(str(ts), p, exposure_of(p))
            log.info(
                "position snapshot written ts=%s market=%s outcome=%s qty=%.4f avg=%.4f exp=%.4f rpnl=%.4f upnl=%.4f",
                ts.isoformat(),
                p.market_id,
                p.outcome_id,
                p.qty,
                p.avg_price,
                exposure_of(p),
                p.realized_pnl,
                p.unrealized_pnl,
            )
            self.last_position_snapshot_sig[(p.market_id, p.outcome_id)] = sig

    async def _refresh_positions(self, *, reason: str) -> None:
        fresh_positions = await self.ctx.exchange.fetch_positions()
        for p in fresh_positions:
            self._normalize_position(p)
        self.ctx.state.positions = {(p.market_id, p.outcome_id): p for p in fresh_positions}
        log.info("positions refreshed reason=%s count=%d", reason, len(self.ctx.state.positions))
        for p in fresh_positions:
            log.info(
                "position reconciled market=%s outcome=%s qty=%.4f avg=%.4f rpnl=%.4f upnl=%.4f",
                p.market_id,
                p.outcome_id,
                p.qty,
                p.avg_price,
                p.realized_pnl,
                p.unrealized_pnl,
            )

    @staticmethod
    def _has_unexpected_position_drift(
        prev_sig: tuple[float, float, float, float, float],
        sig: tuple[float, float, float, float, float],
    ) -> bool:
        # Normal fills and mark-to-market updates should evolve fields over time; only flag impossible states.
        qty, avg, exp, _, _ = sig
        if abs(qty) <= 1e-8 and (abs(avg) > 1e-8 or abs(exp) > 1e-8):
            return True
        if abs(exp - abs(qty * avg)) > 1e-6:
            return True
        # Detect NaN/inf-like numeric instability using self-inequality and huge bounds.
        for v in sig:
            if v != v or abs(v) > 1e12:
                return True
        return False

    @staticmethod
    def _normalize_position(p) -> None:
        if abs(float(p.qty)) <= 1e-8:
            p.qty = 0.0
            p.avg_price = 0.0
            p.unrealized_pnl = 0.0

    async def shutdown(self):
        self.running = False
        if self.ctx.settings.env.cancel_all_on_exit and self.ctx.settings.env.mode.value == "LIVE":
            await self.ctx.exec.cancel_all()
        await self.ctx.notifier.send("bot stop")
