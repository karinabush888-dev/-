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
        self.order_tags: dict[str, str] = {}

    async def place_limit(self, market_id: str, outcome_id: str, side: Side, price: float, size: float, *, tag: str = "generic"):
        if size <= 0:
            raise ValueError("order size must be > 0")
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
        self.order_tags[o.order_id] = tag
        if o.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
            await self.notifier.send(
                f"order not admitted {o.order_id} status={o.status.value} {side.value} {size}@{price:.4f} {market_id}/{outcome_id} tag={tag}"
            )
        else:
            await self.notifier.send(f"order placed {o.order_id} {side.value} {size}@{price:.4f} {market_id}/{outcome_id} tag={tag}")
        return o

    async def cancel(self, order_id: str, *, reason: str = ""):
        ok = await self.exchange.cancel_order(order_id)
        now = str(utc_now())
        if ok:
            await self.repo.upsert_order_status(order_id, OrderStatus.CANCELED.value, now)
            self.order_tags.pop(order_id, None)
            await self.notifier.send(f"order canceled {order_id}{' reason=' + reason if reason else ''}")
            return True

        # Fallback reconciliation: if not open upstream anymore, close stale DB row to avoid OPEN drift.
        exchange_open_ids = {o.order_id for o in await self.exchange.fetch_open_orders()}
        if order_id not in exchange_open_ids:
            await self.repo.upsert_order_status(order_id, OrderStatus.CANCELED.value, now)
            self.order_tags.pop(order_id, None)
            await self.notifier.send(f"order cancel reconciled {order_id}{' reason=' + reason if reason else ''}")
            return True
        return False

    async def cancel_all(self):
        open_orders = await self.repo.get_open_orders()
        canceled_ids: list[str] = []
        for o in open_orders:
            if await self.exchange.cancel_order(o.order_id):
                canceled_ids.append(o.order_id)
                self.order_tags.pop(o.order_id, None)
        await self.repo.bulk_update_order_status(canceled_ids, OrderStatus.CANCELED.value, str(utc_now()))
        await self.reconcile_open_orders()
        await self.notifier.send(f"cancel all orders requested={len(open_orders)} canceled={len(canceled_ids)}")
        return len(canceled_ids)

    async def reconcile_open_orders(self) -> None:
        exchange_open = await self.exchange.fetch_open_orders()
        exchange_open_ids = {o.order_id for o in exchange_open}
        db_open = await self.repo.get_open_orders()
        now = str(utc_now())
        for o in db_open:
            if o.order_id not in exchange_open_ids:
                await self.repo.upsert_order_status(o.order_id, OrderStatus.CANCELED.value, now)
                self.order_tags.pop(o.order_id, None)

    async def cancel_market_orders(self, market_id: str, outcome_id: str | None = None, *, reason: str = "", tag_filter: set[str] | None = None) -> int:
        open_orders = await self.repo.get_open_orders(market_id=market_id, outcome_id=outcome_id)
        if tag_filter is not None:
            open_orders = [o for o in open_orders if self.order_tags.get(o.order_id) in tag_filter]
        canceled_ids: list[str] = []
        for o in open_orders:
            if await self.exchange.cancel_order(o.order_id):
                canceled_ids.append(o.order_id)
                self.order_tags.pop(o.order_id, None)
        await self.repo.bulk_update_order_status(canceled_ids, OrderStatus.CANCELED.value, str(utc_now()))
        await self.reconcile_open_orders()
        if canceled_ids:
            suffix = f" reason={reason}" if reason else ""
            await self.notifier.send(f"canceled {len(canceled_ids)} orders for market={market_id} outcome={outcome_id or '*'}{suffix}")
        return len(canceled_ids)

    async def replace_limit(
        self,
        old_order_id: str,
        market_id: str,
        outcome_id: str,
        side: Side,
        price: float,
        size: float,
        *,
        old_tag: str | None = None,
        new_tag: str | None = None,
    ):
        canceled = await self.cancel(old_order_id, reason="replace")
        if not canceled:
            raise RuntimeError(f"replace failed; could not cancel old order {old_order_id}")
        if old_tag:
            self.order_tags.pop(old_order_id, None)
        return await self.place_limit(market_id, outcome_id, side, price, size, tag=new_tag or old_tag or "replace")
