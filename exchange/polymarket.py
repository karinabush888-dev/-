from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from core.models import Fill, Market, Order, OrderBook, OrderRequest, Outcome, Position
from core.types import OrderStatus, Side
from exchange.base import ExchangeClient


class LivePolymarketClient(ExchangeClient):
    def __init__(
        self,
        api_base: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        timeout_sec: int = 15,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.client = httpx.AsyncClient(timeout=timeout_sec)

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "X-API-KEY": self.api_key,
            "X-API-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        r = await self.client.request(method, f"{self.api_base}{path}", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data
        return {"data": data}

    async def fetch_markets(self) -> list[Market]:
        data = await self._request("GET", "/markets")
        items = data.get("data", data.get("markets", []))
        markets: list[Market] = []
        for m in items:
            outcomes = [
                Outcome(
                    outcome_id=str(o.get("id", o.get("token_id"))),
                    label=str(o.get("label", o.get("name", "outcome"))),
                    implied_prob=float(o.get("probability", o.get("price", 0.5))),
                    volume=float(o.get("volume", 0.0)),
                )
                for o in m.get("outcomes", [])
            ]
            markets.append(
                Market(
                    market_id=str(m.get("id", m.get("condition_id"))),
                    name=str(m.get("name", "market")),
                    event_url=str(m.get("slug", "")),
                    outcomes=outcomes,
                )
            )
        return markets

    async def fetch_market_detail(self, market_id: str) -> Market:
        data = await self._request("GET", f"/markets/{market_id}")
        m = data.get("data", data)
        outcomes = [
            Outcome(
                outcome_id=str(o.get("id", o.get("token_id"))),
                label=str(o.get("label", o.get("name", "outcome"))),
                implied_prob=float(o.get("probability", o.get("price", 0.5))),
                volume=float(o.get("volume", 0.0)),
            )
            for o in m.get("outcomes", [])
        ]
        return Market(market_id=str(m.get("id", market_id)), name=str(m.get("name", "market")), event_url=str(m.get("slug", "")), outcomes=outcomes)

    async def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook:
        data = await self._request("GET", f"/book?market={market_id}&token_id={outcome_id}")
        b = data.get("data", data)
        bids = b.get("bids", [])
        asks = b.get("asks", [])
        best_bid = float(bids[0][0] if bids else 0.01)
        best_ask = float(asks[0][0] if asks else 0.99)
        bid_size = float(bids[0][1] if bids else 0)
        ask_size = float(asks[0][1] if asks else 0)
        return OrderBook(market_id=market_id, outcome_id=outcome_id, best_bid=best_bid, best_ask=best_ask, bid_size=bid_size, ask_size=ask_size)

    async def fetch_positions(self) -> list[Position]:
        data = await self._request("GET", "/positions")
        out: list[Position] = []
        for p in data.get("data", data.get("positions", [])):
            out.append(
                Position(
                    market_id=str(p.get("market_id")),
                    outcome_id=str(p.get("outcome_id", p.get("token_id"))),
                    qty=float(p.get("qty", p.get("size", 0))),
                    avg_price=float(p.get("avg_price", 0.0)),
                    realized_pnl=float(p.get("realized_pnl", 0.0)),
                    unrealized_pnl=float(p.get("unrealized_pnl", 0.0)),
                )
            )
        return out

    async def fetch_balance(self) -> float:
        data = await self._request("GET", "/balance")
        return float(data.get("balance", data.get("available", 0.0)))

    async def place_order(self, req: OrderRequest) -> Order:
        payload = {
            "market_id": req.market_id,
            "outcome_id": req.outcome_id,
            "side": req.side.value.lower(),
            "price": req.price,
            "size": req.size,
            "client_order_id": req.client_id,
        }
        data = await self._request("POST", "/orders", payload)
        o = data.get("data", data)
        return Order(
            order_id=str(o.get("id", req.client_id)),
            market_id=req.market_id,
            outcome_id=req.outcome_id,
            side=req.side,
            price=float(o.get("price", req.price)),
            size=float(o.get("size", req.size)),
            filled_size=float(o.get("filled_size", 0.0)),
            status=OrderStatus(str(o.get("status", "OPEN")).upper()),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def cancel_order(self, order_id: str) -> bool:
        await self._request("DELETE", f"/orders/{order_id}")
        return True

    async def cancel_all_orders(self) -> int:
        data = await self._request("DELETE", "/orders")
        return int(data.get("canceled", 0))

    async def fetch_open_orders(self) -> list[Order]:
        data = await self._request("GET", "/orders?status=open")
        out: list[Order] = []
        for o in data.get("data", data.get("orders", [])):
            out.append(
                Order(
                    order_id=str(o.get("id")),
                    market_id=str(o.get("market_id")),
                    outcome_id=str(o.get("outcome_id", o.get("token_id"))),
                    side=Side(str(o.get("side", "BUY")).upper()),
                    price=float(o.get("price", 0.0)),
                    size=float(o.get("size", 0.0)),
                    filled_size=float(o.get("filled_size", 0.0)),
                    status=OrderStatus(str(o.get("status", "OPEN")).upper()),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        return out

    async def fetch_fills(self, since: datetime | None = None) -> list[Fill]:
        data = await self._request("GET", "/fills")
        out: list[Fill] = []
        for f in data.get("data", data.get("fills", [])):
            ts = datetime.fromisoformat(str(f.get("ts", datetime.now(UTC).isoformat())).replace("Z", "+00:00"))
            if since and ts <= since:
                continue
            out.append(
                Fill(
                    fill_id=str(f.get("id")),
                    order_id=str(f.get("order_id")),
                    market_id=str(f.get("market_id")),
                    outcome_id=str(f.get("outcome_id", f.get("token_id"))),
                    side=Side(str(f.get("side", "BUY")).upper()),
                    price=float(f.get("price", 0.0)),
                    size=float(f.get("size", 0.0)),
                    fee=float(f.get("fee", 0.0)),
                    ts=ts,
                )
            )
        return out

    async def get_server_time(self) -> datetime:
        data = await self._request("GET", "/time")
        val = data.get("iso", data.get("server_time", datetime.now(UTC).isoformat()))
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))

    async def get_market_resolution_time(self, market_id: str) -> datetime | None:
        m = await self.fetch_market_detail(market_id)
        if isinstance(m.event_url, str):
            return None
        return None
