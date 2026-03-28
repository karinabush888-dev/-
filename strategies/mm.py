from __future__ import annotations

from core.types import Side
from exchange.orderbook import quote_half_spread


class MarketMakingStrategy:
    def __init__(self, risk_cfg) -> None:
        self.risk_cfg = risk_cfg

    def build_quotes(self, book, pos_exposure: float, max_exposure: float) -> tuple[tuple[Side, float], tuple[Side, float], bool]:
        hs = quote_half_spread(book)
        mid = book.mid
        skew = 0.0
        reduce_only = False
        ratio = 0.0 if max_exposure <= 0 else pos_exposure / max_exposure
        if ratio > self.risk_cfg.mm_inventory_skew_trigger_pct:
            skew = hs * 0.5
        if ratio > self.risk_cfg.mm_reduce_only_trigger_pct:
            reduce_only = True
        bid = max(0.01, mid - hs - skew)
        ask = min(0.99, mid + hs + skew)
        return (Side.BUY, bid), (Side.SELL, ask), reduce_only
