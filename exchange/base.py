from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import Fill, Market, Order, OrderBook, OrderRequest, Position


class ExchangeClient(ABC):
    @abstractmethod
    async def fetch_markets(self) -> list[Market]: ...

    @abstractmethod
    async def fetch_market_detail(self, market_id: str) -> Market: ...

    @abstractmethod
    async def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook: ...

    @abstractmethod
    async def fetch_positions(self) -> list[Position]: ...

    @abstractmethod
    async def fetch_balance(self) -> float: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def cancel_all_orders(self) -> int: ...

    @abstractmethod
    async def fetch_open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def fetch_fills(self, since: datetime | None = None) -> list[Fill]: ...

    @abstractmethod
    async def get_server_time(self) -> datetime: ...

    @abstractmethod
    async def get_market_resolution_time(self, market_id: str) -> datetime | None: ...
