from __future__ import annotations

from datetime import timedelta

from core.state import RuntimeState
from core.timeutils import utc_now
from risk.limits import DynamicSizing
from utils.math_utils import clamp


class RiskEngine:
    def __init__(self, risk_cfg) -> None:
        self.cfg = risk_cfg

    def dynamic_sizing(self, equity: float, mode_multiplier_mm: float = 1.0, mode_multiplier_mis: float = 1.0) -> DynamicSizing:
        mm_alloc = self.cfg.mm_allocation_pct * equity
        mis_alloc = self.cfg.mis_allocation_pct * equity
        mm = clamp(self.cfg.order_size_mm_pct * equity * mode_multiplier_mm, self.cfg.order_size_mm_min, self.cfg.order_size_mm_max)
        mis = clamp(self.cfg.order_size_mis_pct * equity * mode_multiplier_mis, self.cfg.order_size_mis_min, self.cfg.order_size_mis_max)
        max_exp = self.cfg.max_exposure_per_outcome_pct * equity
        return DynamicSizing(mm_alloc, mis_alloc, mm, mis, max_exp)

    def kill_switch_reason(self, pnl_today: float, equity: float, state: RuntimeState) -> str | None:
        daily_loss_limit_value = self.cfg.daily_loss_limit_pct * equity
        if pnl_today <= -daily_loss_limit_value:
            return f"daily_loss_limit breached pnl_today={pnl_today:.4f} limit=-{daily_loss_limit_value:.4f}"
        if state.stats.stopouts_today >= self.cfg.max_stopouts_per_day:
            return f"max_stopouts_per_day reached stopouts_today={state.stats.stopouts_today} limit={self.cfg.max_stopouts_per_day}"
        return None

    def should_kill_switch(self, pnl_today: float, equity: float, state: RuntimeState) -> bool:
        return self.kill_switch_reason(pnl_today, equity, state) is not None

    def activate_pause_to_next_day(self, state: RuntimeState, reason: str) -> None:
        now = utc_now()
        next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        state.pause_until = next_day
        state.kill_switch_active = True
        state.pause_reason = reason

    def near_resolution(self, resolution_ts, pause_before_resolution_minutes: int) -> bool:
        if resolution_ts is None:
            return False
        return (resolution_ts - utc_now()).total_seconds() <= pause_before_resolution_minutes * 60
