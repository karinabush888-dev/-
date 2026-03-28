from __future__ import annotations

import aiosqlite

from core.models import Fill, Order, Position
from core.types import OrderStatus, Side


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def insert_order(self, o: Order) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                (o.order_id, o.market_id, o.outcome_id, o.side.value, o.price, o.size, o.filled_size, o.status.value, str(o.created_at), str(o.updated_at)),
            )
            await db.commit()

    async def upsert_order_status(self, order_id: str, status: str, updated_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE orders SET status=?, updated_at=? WHERE order_id=?", (status, updated_at, order_id))
            await db.commit()

    async def get_open_orders(self, market_id: str | None = None, outcome_id: str | None = None) -> list[Order]:
        query = "SELECT order_id, market_id, outcome_id, side, price, size, filled_size, status, created_at, updated_at FROM orders WHERE status IN (?,?)"
        params: list[object] = [OrderStatus.OPEN.value, OrderStatus.PARTIAL.value]
        if market_id:
            query += " AND market_id=?"
            params.append(market_id)
        if outcome_id:
            query += " AND outcome_id=?"
            params.append(outcome_id)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
        return [
            Order(
                order_id=r[0],
                market_id=r[1],
                outcome_id=r[2],
                side=Side(r[3]),
                price=float(r[4]),
                size=float(r[5]),
                filled_size=float(r[6] or 0.0),
                status=OrderStatus(r[7]),
                created_at=r[8],
                updated_at=r[9],
            )
            for r in rows
        ]

    async def apply_fill_to_order(self, order_id: str, fill_size: float, updated_at: str) -> tuple[float, float, str] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT size, filled_size, status FROM orders WHERE order_id=?", (order_id,))
            row = await cur.fetchone()
            if row is None:
                return None
            total = float(row[0] or 0.0)
            current = float(row[1] or 0.0)
            new_filled = min(total, round(current + float(fill_size), 8))
            if new_filled <= 0:
                status = row[2]
            elif new_filled + 1e-9 >= total:
                status = OrderStatus.FILLED.value
            else:
                status = OrderStatus.PARTIAL.value
            await db.execute(
                "UPDATE orders SET filled_size=?, status=?, updated_at=? WHERE order_id=?",
                (new_filled, status, updated_at, order_id),
            )
            await db.commit()
            return total, new_filled, status

    async def bulk_update_order_status(self, order_ids: list[str], status: str, updated_at: str) -> None:
        if not order_ids:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
                [(status, updated_at, oid) for oid in order_ids],
            )
            await db.commit()

    async def insert_fill(self, f: Fill) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO fills (fill_id, order_id, market_id, outcome_id, side, price, size, fee, ts, reconciled) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f.fill_id, f.order_id, f.market_id, f.outcome_id, f.side.value, f.price, f.size, f.fee, str(f.ts)),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def is_fill_reconciled(self, fill_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT reconciled FROM fills WHERE fill_id=?", (fill_id,))
            row = await cur.fetchone()
            return bool(row and int(row[0] or 0) == 1)

    async def mark_fill_reconciled(self, fill_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE fills SET reconciled=1 WHERE fill_id=?", (fill_id,))
            await db.commit()

    async def snapshot_position(self, ts: str, p: Position, exposure: float) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO positions_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (ts, p.market_id, p.outcome_id, p.qty, p.avg_price, exposure, p.unrealized_pnl, p.realized_pnl),
            )
            await db.commit()

    async def snapshot_pnl(self, ts: str, equity: float, pnl_today: float, pnl_mtd: float, progress: float, mode: str, drawdown: float) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO pnl_snapshots VALUES (?,?,?,?,?,?,?)",
                (ts, equity, pnl_today, pnl_mtd, progress, mode, drawdown),
            )
            await db.commit()

    async def set_bot_state(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO bot_state VALUES (?,?)", (key, value))
            await db.commit()

    async def get_bot_state(self, key: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM bot_state WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def save_selected_market(self, market_id: str, market_name: str, outcome_id: str, outcome_label: str, prob: float, liquidity: float, selected_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO selected_markets VALUES (?,?,?,?,?,?,?)",
                (market_id, market_name, outcome_id, outcome_label, prob, liquidity, selected_at),
            )
            await db.commit()

    async def get_last_n_daily_pnl(self, n: int) -> list[float]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT pnl_day FROM daily_metrics ORDER BY day_key DESC LIMIT ?", (n,))
            rows = await cur.fetchall()
            return [float(r[0]) for r in rows]

    async def upsert_daily_metrics(self, day_key: str, trades: int, stopouts: int, mispricing_trades: int, pnl_day: float) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO daily_metrics VALUES (?,?,?,?,?)",
                (day_key, trades, stopouts, mispricing_trades, pnl_day),
            )
            await db.commit()
