from __future__ import annotations

from datetime import datetime

from core.models import Position


class PnLEngine:
    def __init__(self, starting_equity: float) -> None:
        self.starting_equity = starting_equity
        self.equity_start_day = starting_equity
        self.equity_start_month = starting_equity
        self.peak_equity = starting_equity

    def mark_to_market(self, cash: float, positions: list[Position], mids: dict[tuple[str, str], float]) -> tuple[float, float]:
        unreal = 0.0
        for p in positions:
            mid = mids.get((p.market_id, p.outcome_id), p.avg_price)
            p.unrealized_pnl = p.qty * (mid - p.avg_price)
            unreal += p.unrealized_pnl
        equity = cash + sum(p.qty * mids.get((p.market_id, p.outcome_id), p.avg_price) for p in positions)
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 0.0 if self.peak_equity == 0 else (self.peak_equity - equity) / self.peak_equity
        return equity, drawdown

    def pnl_today(self, equity: float) -> float:
        return equity - self.equity_start_day

    def pnl_mtd(self, equity: float) -> float:
        return equity - self.equity_start_month

    def progress_to_goal_500(self, equity: float) -> float:
        return equity / 500 * 100

    def reset_day(self, equity: float) -> None:
        self.equity_start_day = equity

    def maybe_reset_month(self, now: datetime, equity: float) -> None:
        if now.day == 1:
            self.equity_start_month = equity
