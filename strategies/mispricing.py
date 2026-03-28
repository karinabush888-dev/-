from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from core.models import Fill, MispricingTrade
from core.timeutils import utc_now
from core.types import Side


@dataclass
class MispricingExitAction:
    side: Side
    size: float
    reason: str


class MispricingStrategy:
    def __init__(self, risk_cfg) -> None:
        self.cfg = risk_cfg
        self.history: dict[tuple[str, str], deque[tuple]] = {}
        self.active_trades: dict[tuple[str, str], MispricingTrade] = {}
        self.pending_entry_orders: dict[str, dict[str, float | str | Side]] = {}
        self.pending_exit_orders: dict[tuple[str, str], dict[str, float | str | Side]] = {}

    def on_tick(self, market_id: str, outcome_id: str, mid: float):
        key = (market_id, outcome_id)
        if key not in self.history:
            self.history[key] = deque(maxlen=600)
        now = utc_now()
        self.history[key].append((now, mid))

    def detect_signal(self, market_id: str, outcome_id: str) -> Side | None:
        key = (market_id, outcome_id)
        h = self.history.get(key)
        if not h or len(h) < 20:
            return None
        now = utc_now()
        window = [x for x in h if (now - x[0]).total_seconds() <= 300]
        if len(window) < 10:
            return None
        prices = [p for _, p in window]
        move = max(prices) - min(prices)
        if move < 0.10:
            return None
        extreme_ts = window[prices.index(max(prices))][0] if prices[-1] < prices[0] else window[prices.index(min(prices))][0]
        if (now - extreme_ts) < timedelta(minutes=2):
            return None
        return Side.BUY if prices[-1] > prices[0] else Side.SELL

    def register_entry_order(self, order_id: str, market_id: str, outcome_id: str, side: Side) -> None:
        self.pending_entry_orders[order_id] = {"market_id": market_id, "outcome_id": outcome_id, "side": side, "placed_at_ts": utc_now().timestamp()}

    def register_exit_order(self, order_id: str, market_id: str, outcome_id: str, side: Side, reason: str, target_size: float) -> None:
        key = (market_id, outcome_id)
        now_ts = utc_now().timestamp()
        self.pending_exit_orders[key] = {
            "order_id": order_id,
            "side": side,
            "reason": reason,
            "target_size": float(target_size),
            "filled_size": 0.0,
            "placed_at_ts": now_ts,
            "last_progress_ts": now_ts,
        }

    def has_pending_entry(self, market_id: str, outcome_id: str) -> bool:
        key = (market_id, outcome_id)
        stale_ids: list[str] = []
        for oid, pending in self.pending_entry_orders.items():
            age_sec = utc_now().timestamp() - float(pending.get("placed_at_ts", 0.0))
            if age_sec > 180:
                stale_ids.append(oid)
        for oid in stale_ids:
            self.pending_entry_orders.pop(oid, None)
        return any((str(p.get("market_id")), str(p.get("outcome_id"))) == key for p in self.pending_entry_orders.values())

    def has_active_trade(self, market_id: str, outcome_id: str) -> bool:
        trade = self.active_trades.get((market_id, outcome_id))
        return bool(trade and not trade.closed)

    def has_pending_exit(self, market_id: str, outcome_id: str) -> bool:
        key = (market_id, outcome_id)
        pending = self.pending_exit_orders.get(key)
        if not pending:
            return False
        # Allow retry if pending exit has not made progress for a full refresh window * 3.
        # This intentionally applies even to partial fills so canceled/rejected remainders can be re-posted.
        stale_after = 90
        now_ts = utc_now().timestamp()
        last_progress_ts = float(pending.get("last_progress_ts", pending["placed_at_ts"]))
        age_since_progress_sec = now_ts - last_progress_ts
        if age_since_progress_sec > stale_after and float(pending["filled_size"]) < float(pending["target_size"]):
            self.pending_exit_orders.pop(key, None)
            return False
        return True

    def manage_trade(self, market_id: str, outcome_id: str, current_price: float) -> list[MispricingExitAction]:
        key = (market_id, outcome_id)
        trade = self.active_trades.get(key)
        if trade is None or trade.closed or trade.remaining_size <= 0:
            return []
        if self.has_pending_exit(market_id, outcome_id):
            return []

        actions: list[MispricingExitAction] = []
        signed_ret = ((current_price - trade.entry_price) / trade.entry_price) if trade.entry_price > 0 else 0.0
        if trade.side == Side.SELL:
            signed_ret *= -1
        exit_side = Side.SELL if trade.side == Side.BUY else Side.BUY
        now = utc_now()

        tp1_target = round(trade.size * self.cfg.mis_tp1_close_pct, 4)
        tp1_filled = float(trade.meta.get("tp1_filled", 0.0))

        if signed_ret <= -self.cfg.mis_stop_pct and not trade.stop_hit:
            size = round(trade.remaining_size, 4)
            if size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=size, reason="stop"))
            return actions

        if trade.time_stop_deadline and now >= trade.time_stop_deadline and not trade.time_stop_hit:
            size = round(trade.remaining_size, 4)
            if size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=size, reason="time_stop"))
            return actions

        if signed_ret >= self.cfg.mis_tp2_pct and not trade.tp2_hit:
            close_size = round(trade.remaining_size, 4)
            if close_size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=close_size, reason="tp2"))
            return actions

        if signed_ret >= self.cfg.mis_tp1_pct and not trade.tp1_hit:
            close_size = round(min(trade.remaining_size, max(0.0, tp1_target - tp1_filled)), 4)
            if close_size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=close_size, reason="tp1"))
            return actions

        return actions

    def apply_fill(self, fill: Fill) -> dict[str, str | bool | float] | None:
        opened = False
        key: tuple[str, str] | None = None

        entry = self.pending_entry_orders.get(fill.order_id)
        if entry:
            market_id = str(entry["market_id"])
            outcome_id = str(entry["outcome_id"])
            side = entry["side"] if isinstance(entry["side"], Side) else Side(str(entry["side"]))
            key = (market_id, outcome_id)
            trade = self.active_trades.get(key)
            if trade is None or trade.closed:
                trade = MispricingTrade(
                    market_id=market_id,
                    outcome_id=outcome_id,
                    side=side,
                    entry_price=fill.price,
                    entry_ts=fill.ts,
                    size=0.0,
                    remaining_size=0.0,
                    time_stop_deadline=fill.ts + timedelta(minutes=self.cfg.mis_time_stop_minutes),
                )
                self.active_trades[key] = trade
                opened = True

            previous_size = trade.size
            trade.size = round(trade.size + fill.size, 4)
            trade.remaining_size = round(trade.remaining_size + fill.size, 4)
            trade.entry_price = ((trade.entry_price * previous_size) + (fill.price * fill.size)) / trade.size if trade.size > 0 else trade.entry_price

        if key is None:
            key = (fill.market_id, fill.outcome_id)
        trade = self.active_trades.get(key)
        if trade is None or trade.closed:
            return None

        exit_pending = self.pending_exit_orders.get(key)
        stop_hit_transition = False
        if fill.side != trade.side:
            closed_size = round(min(fill.size, trade.remaining_size), 4)
            if closed_size > 0:
                trade.remaining_size = round(max(0.0, trade.remaining_size - closed_size), 4)
                if exit_pending and exit_pending.get("order_id") == fill.order_id:
                    reason = str(exit_pending.get("reason"))
                    exit_pending["filled_size"] = round(float(exit_pending.get("filled_size", 0.0)) + closed_size, 4)
                    if reason == "tp1":
                        trade.meta["tp1_filled"] = round(float(trade.meta.get("tp1_filled", 0.0)) + closed_size, 4)
                        if float(trade.meta["tp1_filled"]) >= round(trade.size * self.cfg.mis_tp1_close_pct, 4):
                            trade.tp1_hit = True
                    elif reason == "tp2":
                        trade.tp2_hit = True
                    elif reason == "stop":
                        stop_hit_transition = not trade.stop_hit
                        trade.stop_hit = True
                    elif reason == "time_stop":
                        trade.time_stop_hit = True

                    exit_pending["last_progress_ts"] = utc_now().timestamp()
                    if float(exit_pending["filled_size"]) + 1e-9 >= float(exit_pending["target_size"]):
                        self.pending_exit_orders.pop(key, None)

        if trade.remaining_size <= 0:
            trade.closed = True
            self.pending_exit_orders.pop(key, None)

        return {
            "opened": opened,
            "closed": trade.closed,
            "stop_hit": trade.stop_hit,
            "stop_hit_transition": stop_hit_transition,
            "time_stop_hit": trade.time_stop_hit,
            "remaining_size": trade.remaining_size,
            "entry_price": trade.entry_price,
        }
