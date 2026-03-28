from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.models import BotStats, MispricingTrade, Position
from core.timeutils import utc_day_key, utc_now


@dataclass
class RuntimeState:
    selected_outcomes: dict[str, str] = field(default_factory=dict)
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    open_order_ids: set[str] = field(default_factory=set)
    pause_until: datetime | None = None
    kill_switch_active: bool = False
    mispricing_trades: dict[tuple[str, str], MispricingTrade] = field(default_factory=dict)
    stats: BotStats = field(default_factory=lambda: BotStats(day_key=utc_day_key()))

    def reset_daily(self) -> None:
        self.stats.day_key = utc_day_key()
        self.stats.trades_today = 0
        self.stats.stopouts_today = 0
        self.stats.mispricing_trades_today = 0
        self.kill_switch_active = False
        self.pause_until = None

    def is_paused(self) -> bool:
        return self.pause_until is not None and utc_now() < self.pause_until
