from __future__ import annotations

from uuid import uuid4

from core.timeutils import utc_now
from core.models import OrderRequest
from core.types import OrderStatus, Side


class ExecutionManager:
    def __init__(self, exchange, repo, notifier) -> None:
        self.exchange = exchange
        self.repo = repo
        self.notifier = notifier

    async def place_limit(self, market_id: str, outcome_id: str, side: Side, price: float, size: float):
        req = OrderRequest(
            market_id=market_id,
            outcome_id=outcome_id,
            side=side,
            price=round(price, 4),
            size=round(size, 4),
            client_id=str(uuid4()),
        )
        o = await self.exchange.place_order(req)
        await self.repo.insert_order(o)
        await self.notifier.send(f"order placed {o.order_id} {side.value} {size}@{price:.4f} {market_id}/{outcome_id}")
        return o

    async def cancel(self, order_id: str):
        ok = await self.exchange.cancel_order(order_id)
        if ok:
            await self.repo.upsert_order_status(order_id, OrderStatus.CANCELED.value, str(utc_now()))
            await self.notifier.send(f"order canceled {order_id}")
        return ok

    async def cancel_all(self):
        open_orders = await self.repo.get_open_orders()
        n = await self.exchange.cancel_all_orders()
        await self.repo.bulk_update_order_status([o.order_id for o in open_orders], OrderStatus.CANCELED.value, str(utc_now()))
        await self.notifier.send(f"cancel all orders: {n}")
        return n

    async def cancel_market_orders(self, market_id: str, outcome_id: str | None = None) -> int:
        open_orders = await self.repo.get_open_orders(market_id=market_id, outcome_id=outcome_id)
        canceled = 0
        for o in open_orders:
            if await self.exchange.cancel_order(o.order_id):
                canceled += 1
        await self.repo.bulk_update_order_status([o.order_id for o in open_orders], OrderStatus.CANCELED.value, str(utc_now()))
        if canceled:
            await self.notifier.send(f"canceled {canceled} orders for market={market_id} outcome={outcome_id or '*'}")
        return canceled

    async def replace_limit(
        self,
        old_order_id: str,
        market_id: str,
        outcome_id: str,
        side: Side,
        price: float,
        size: float,
    ):
        await self.cancel(old_order_id)
        return await self.place_limit(market_id, outcome_id, side, price, size)
