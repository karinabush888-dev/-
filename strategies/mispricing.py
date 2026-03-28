from __future__ import annotations

from collections import deque
from datetime import timedelta

from core.timeutils import utc_now
from core.types import Side


class MispricingStrategy:
    def __init__(self, risk_cfg) -> None:
        self.cfg = risk_cfg
        self.history: dict[tuple[str, str], deque[tuple]] = {}

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
