from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.types import AdaptationMode, OrderStatus, Side


@dataclass
class Outcome:
    outcome_id: str
    label: str
    implied_prob: float
    volume: float


@dataclass
class Market:
    market_id: str
    name: str
    event_url: str
    outcomes: list[Outcome]


@dataclass
class OrderBook:
    market_id: str
    outcome_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


@dataclass
class OrderRequest:
    market_id: str
    outcome_id: str
    side: Side
    price: float
    size: float
    client_id: str


@dataclass
class Order:
    order_id: str
    market_id: str
    outcome_id: str
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Fill:
    fill_id: str
    order_id: str
    market_id: str
    outcome_id: str
    side: Side
    price: float
    size: float
    fee: float
    ts: datetime


@dataclass
class Position:
    market_id: str
    outcome_id: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class PnLState:
    equity: float
    cash: float
    pnl_today: float
    pnl_mtd: float
    drawdown: float
    progress_to_goal_500: float


@dataclass
class MispricingTrade:
    market_id: str
    outcome_id: str
    side: Side
    entry_price: float
    entry_ts: datetime
    size: float
    remaining_size: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    stop_hit: bool = False
    time_stop_hit: bool = False
    time_stop_deadline: datetime | None = None
    closed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotStats:
    day_key: str
    trades_today: int = 0
    stopouts_today: int = 0
    mispricing_trades_today: int = 0
    mode: AdaptationMode = AdaptationMode.NORMAL
