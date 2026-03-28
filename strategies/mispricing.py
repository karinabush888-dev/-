from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from core.models import MispricingTrade
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

    def exits(self, entry_price: float, current_price: float) -> tuple[bool, bool, bool]:
        ret = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        tp1 = ret >= self.cfg.mis_tp1_pct
        tp2 = ret >= self.cfg.mis_tp2_pct
        stop = ret <= -self.cfg.mis_stop_pct
        return tp1, tp2, stop

    def start_trade(self, market_id: str, outcome_id: str, side: Side, entry_price: float, size: float) -> MispricingTrade:
        key = (market_id, outcome_id)
        trade = MispricingTrade(
            market_id=market_id,
            outcome_id=outcome_id,
            side=side,
            entry_price=entry_price,
            entry_ts=utc_now(),
            size=size,
            remaining_size=size,
            time_stop_deadline=utc_now() + timedelta(minutes=self.cfg.mis_time_stop_minutes),
        )
        self.active_trades[key] = trade
        return trade

    def record_entry_fill(self, market_id: str, outcome_id: str, side: Side, fill_price: float, fill_size: float) -> MispricingTrade:
        key = (market_id, outcome_id)
        trade = self.active_trades.get(key)
        if trade and not trade.closed and trade.side == side:
            total_size = trade.size + fill_size
            if total_size > 0:
                trade.entry_price = ((trade.entry_price * trade.size) + (fill_price * fill_size)) / total_size
            trade.size = round(total_size, 4)
            trade.remaining_size = round(trade.remaining_size + fill_size, 4)
            return trade
        return self.start_trade(market_id, outcome_id, side, fill_price, fill_size)

    def has_active_trade(self, market_id: str, outcome_id: str) -> bool:
        trade = self.active_trades.get((market_id, outcome_id))
        return bool(trade and not trade.closed)

    def manage_trade(self, market_id: str, outcome_id: str, current_price: float) -> list[MispricingExitAction]:
        key = (market_id, outcome_id)
        trade = self.active_trades.get(key)
        if trade is None or trade.closed or trade.remaining_size <= 0:
            return []

        actions: list[MispricingExitAction] = []
        signed_ret = ((current_price - trade.entry_price) / trade.entry_price) if trade.entry_price > 0 else 0.0
        if trade.side == Side.SELL:
            signed_ret *= -1
        exit_side = Side.SELL if trade.side == Side.BUY else Side.BUY
        now = utc_now()

        if signed_ret <= -self.cfg.mis_stop_pct and not trade.stop_hit:
            trade.stop_hit = True
            size = round(trade.remaining_size, 4)
            if size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=size, reason="stop"))
                trade.remaining_size = 0.0
                trade.closed = True
            return actions

        if trade.time_stop_deadline and now >= trade.time_stop_deadline and not trade.time_stop_hit:
            trade.time_stop_hit = True
            size = round(trade.remaining_size, 4)
            if size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=size, reason="time_stop"))
                trade.remaining_size = 0.0
                trade.closed = True
            return actions

        if signed_ret >= self.cfg.mis_tp1_pct and not trade.tp1_hit:
            close_size = min(trade.remaining_size, round(trade.size * self.cfg.mis_tp1_close_pct, 4))
            if close_size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=close_size, reason="tp1"))
                trade.remaining_size = max(0.0, trade.remaining_size - close_size)
                trade.tp1_hit = True

        if signed_ret >= self.cfg.mis_tp2_pct and not trade.tp2_hit:
            close_size = min(trade.remaining_size, round(trade.size * self.cfg.mis_tp2_close_pct, 4))
            if close_size > 0:
                actions.append(MispricingExitAction(side=exit_side, size=close_size, reason="tp2"))
                trade.remaining_size = max(0.0, trade.remaining_size - close_size)
                trade.tp2_hit = True

        if trade.remaining_size <= 0:
            trade.closed = True
        return actions
