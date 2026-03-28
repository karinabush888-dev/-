from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from core.models import Fill, Market, Order, OrderBook, OrderRequest, Outcome, Position
from core.types import OrderStatus, Side
from exchange.base import ExchangeClient

log = logging.getLogger(__name__)


class LivePolymarketClient(ExchangeClient):
    def __init__(
        self,
        api_base: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        private_key: str,
        proxy_address: str,
        funder: str,
        timeout_sec: int = 15,
        max_retries: int = 5,
        retry_backoff_min: float = 0.5,
        retry_backoff_max: float = 8.0,
    ) -> None:
        missing = [k for k, v in {
            "api_key": api_key,
            "api_secret": api_secret,
            "passphrase": passphrase,
            "private_key": private_key,
            "proxy_address": proxy_address,
            "funder": funder,
        }.items() if not v]
        if missing:
            raise ValueError(f"LIVE Polymarket client missing credentials/signing fields: {', '.join(missing)}")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.private_key = private_key
        self.proxy_address = proxy_address
        self.funder = funder
        self.max_retries = max_retries
        self.retry_backoff_min = retry_backoff_min
        self.retry_backoff_max = retry_backoff_max
        self.client = httpx.AsyncClient(timeout=timeout_sec)

        log.warning(
            "LIVE mode is enabled. Endpoint compatibility is assumed for /markets,/book,/orders,/fills,/positions and MUST be validated in staging before real deployment."
        )

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "X-API-KEY": self.api_key,
            "X-API-SECRET": self.api_secret,
            "X-API-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        url = f"{self.api_base}{path}"
        retry = 0
        backoff = self.retry_backoff_min
        while True:
            try:
                r = await self.client.request(method, url, json=payload, headers=headers)
                if r.status_code in {408, 429} or r.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable upstream error", request=r.request, response=r)
                r.raise_for_status()
                try:
                    data = r.json()
                except ValueError as exc:
                    raise RuntimeError(f"Non-JSON response from Polymarket path={path}") from exc
                if isinstance(data, dict):
                    return data
                return {"data": data}
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError, RuntimeError) as exc:
                retry += 1
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) and exc.response else None
                retryable = status_code in {408, 429, 500, 502, 503, 504} or isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
                if (not retryable) or retry >= self.max_retries:
                    raise RuntimeError(f"Polymarket request failed method={method} path={path} retries={retry}: {exc}") from exc
                retry_after = 0.0
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    retry_after = float(exc.response.headers.get("retry-after", "0") or 0)
                wait_s = max(retry_after, backoff)
                log.warning("live api retry method=%s path=%s attempt=%d wait=%.2fs status=%s", method, path, retry, wait_s, status_code)
                await asyncio.sleep(wait_s)
                backoff = min(backoff * 2, self.retry_backoff_max)

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
        # Note: assumes [price,size] tuple format; verify on staging before production use.
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
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    async def cancel_order(self, order_id: str) -> bool:
        await self._request("DELETE", f"/orders/{order_id}")
        return True

    async def cancel_all_orders(self) -> int:
        # Endpoint semantics vary across deployments; rely on per-order cancel upstream when strict consistency is required.
        data = await self._request("DELETE", "/orders")
        if "canceled" not in data:
            log.warning("cancel_all_orders response missing canceled count; treating as 0 and relying on follow-up reconciliation")
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
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
        return out

    async def fetch_fills(self, since: datetime | None = None) -> list[Fill]:
        data = await self._request("GET", "/fills")
        out: list[Fill] = []
        for f in data.get("data", data.get("fills", [])):
            ts = datetime.fromisoformat(str(f.get("ts", datetime.now(timezone.utc).isoformat())).replace("Z", "+00:00"))
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
        val = data.get("iso", data.get("server_time", datetime.now(timezone.utc).isoformat()))
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))

    async def get_market_resolution_time(self, market_id: str) -> datetime | None:
        data = await self._request("GET", f"/markets/{market_id}")
        m = data.get("data", data)
        candidates = (
            m.get("resolution_time"),
            m.get("resolve_time"),
            m.get("resolved_at"),
            m.get("end_date"),
            m.get("endDate"),
            m.get("end_time"),
            m.get("endTime"),
            m.get("expiration_time"),
            m.get("expiry"),
        )
        for value in candidates:
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(float(value), tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
            if isinstance(value, str):
                normalized = value.replace("Z", "+00:00")
                try:
                    parsed = datetime.fromisoformat(normalized)
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        log.warning("market resolution time unavailable for market_id=%s; near-resolution risk controls may be reduced", market_id)
        return None
