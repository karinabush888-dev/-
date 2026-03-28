from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from core.models import Fill, Market, Order, OrderBook, OrderRequest, Outcome, Position
from core.timeutils import utc_now
from core.types import OrderStatus, Side
from exchange.base import ExchangeClient


class PaperExchangeClient(ExchangeClient):
    def __init__(self, starting_equity: float, latency_ms: int = 250) -> None:
        self.cash = starting_equity
        self.latency_ms = latency_ms
        self.markets = self._seed_markets()
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.positions: dict[tuple[str, str], Position] = defaultdict(lambda: Position("", ""))
        self._build_books()

    def _seed_markets(self) -> list[Market]:
        base = [
            ("mkt_btc", "BTC", "https://polymarket.com/event/bitcoin-above-on-april-3"),
            ("mkt_eth", "ETH", "https://polymarket.com/event/ethereum-above-on-april-4"),
        ]
        out: list[Market] = []
        for mid, name, url in base:
            outcomes = [
                Outcome(f"{mid}_o1", "Strike 1", 0.35, 15000),
                Outcome(f"{mid}_o2", "Strike 2", 0.50, 22000),
                Outcome(f"{mid}_o3", "Strike 3", 0.67, 12000),
            ]
            out.append(Market(market_id=mid, name=name, event_url=url, outcomes=outcomes))
        return out

    def _build_books(self) -> None:
        for m in self.markets:
            for o in m.outcomes:
                mid = o.implied_prob
                spread = 0.03
                self.books[(m.market_id, o.outcome_id)] = OrderBook(
                    market_id=m.market_id,
                    outcome_id=o.outcome_id,
                    best_bid=max(0.01, mid - spread / 2),
                    best_ask=min(0.99, mid + spread / 2),
                    bid_size=1000,
                    ask_size=1000,
                )

    async def fetch_markets(self) -> list[Market]:
        return self.markets

    async def fetch_market_detail(self, market_id: str) -> Market:
        return next(m for m in self.markets if m.market_id == market_id)

    async def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook:
        book = self.books[(market_id, outcome_id)]
        drift = random.uniform(-0.01, 0.01)
        mid = min(0.99, max(0.01, book.mid + drift))
        spread = random.uniform(0.02, 0.04)
        book.best_bid = max(0.01, mid - spread / 2)
        book.best_ask = min(0.99, mid + spread / 2)
        return book

    async def fetch_positions(self) -> list[Position]:
        return list(self.positions.values())

    async def fetch_balance(self) -> float:
        return self.cash

    async def place_order(self, req: OrderRequest) -> Order:
        await asyncio.sleep(self.latency_ms / 1000)
        oid = str(uuid4())
        now = utc_now()
        order = Order(
            order_id=oid,
            market_id=req.market_id,
            outcome_id=req.outcome_id,
            side=req.side,
            price=req.price,
            size=req.size,
            created_at=now,
            updated_at=now,
        )
        self.orders[oid] = order
        await self._try_fill(order)
        return order

    async def _try_fill(self, order: Order) -> None:
        book = await self.fetch_orderbook(order.market_id, order.outcome_id)
        instant = (order.side == Side.BUY and order.price >= book.best_ask) or (
            order.side == Side.SELL and order.price <= book.best_bid
        )
        touch = (order.side == Side.BUY and order.price >= book.best_bid) or (
            order.side == Side.SELL and order.price <= book.best_ask
        )
        if not instant and not (touch and random.random() < 0.35):
            return
        frac = random.choice([0.25, 0.5, 1.0])
        fill_size = min(order.size - order.filled_size, round(order.size * frac, 4))
        if fill_size <= 0:
            return
        fee = fill_size * order.price * 0.001
        fill = Fill(
            fill_id=str(uuid4()),
            order_id=order.order_id,
            market_id=order.market_id,
            outcome_id=order.outcome_id,
            side=order.side,
            price=order.price,
            size=fill_size,
            fee=fee,
            ts=utc_now(),
        )
        self.fills.append(fill)
        order.filled_size += fill_size
        order.status = OrderStatus.FILLED if abs(order.filled_size - order.size) < 1e-9 else OrderStatus.PARTIAL
        key = (order.market_id, order.outcome_id)
        pos = self.positions.get(key)
        if pos is None or pos.market_id == "":
            pos = Position(market_id=order.market_id, outcome_id=order.outcome_id)
            self.positions[key] = pos
        signed = fill_size if order.side == Side.BUY else -fill_size
        prev_qty = pos.qty
        if prev_qty == 0 or (prev_qty > 0 and signed > 0) or (prev_qty < 0 and signed < 0):
            total_cost = pos.avg_price * abs(pos.qty) + fill.price * abs(signed)
            pos.qty += signed
            pos.avg_price = total_cost / abs(pos.qty) if pos.qty != 0 else 0.0
        else:
            closing = min(abs(prev_qty), abs(signed))
            pnl = closing * ((fill.price - pos.avg_price) if prev_qty > 0 else (pos.avg_price - fill.price))
            pos.realized_pnl += pnl - fee
            pos.qty += signed
            if pos.qty == 0:
                pos.avg_price = 0.0
            elif (prev_qty > 0 > pos.qty) or (prev_qty < 0 < pos.qty):
                # Position flipped direction on this fill; reset cost basis to fill price.
                pos.avg_price = fill.price
        cash_delta = -fill.size * fill.price - fee if order.side == Side.BUY else fill.size * fill.price - fee
        self.cash += cash_delta

    async def cancel_order(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if not o or o.status == OrderStatus.FILLED:
            return False
        o.status = OrderStatus.CANCELED
        o.updated_at = utc_now()
        return True

    async def cancel_all_orders(self) -> int:
        n = 0
        for o in self.orders.values():
            if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
                o.status = OrderStatus.CANCELED
                n += 1
        return n

    async def fetch_open_orders(self) -> list[Order]:
        for o in list(self.orders.values()):
            if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
                await self._try_fill(o)
        return [o for o in self.orders.values() if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}]

    async def fetch_fills(self, since: datetime | None = None) -> list[Fill]:
        if since is None:
            return list(self.fills)
        return [f for f in self.fills if f.ts > since]

    async def get_server_time(self) -> datetime:
        return utc_now()

    async def get_market_resolution_time(self, market_id: str) -> datetime | None:
        return utc_now().replace(hour=23, minute=59, second=0, microsecond=0)
