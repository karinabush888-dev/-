from __future__ import annotations

import json

import aiosqlite

from core.models import Fill, Order, Position


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def insert_order(self, o: Order) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                (o.order_id, o.market_id, o.outcome_id, o.side.value, o.price, o.size, o.status.value, str(o.created_at), str(o.updated_at)),
            )
            await db.commit()

    async def upsert_order_status(self, order_id: str, status: str, updated_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE orders SET status=?, updated_at=? WHERE order_id=?", (status, updated_at, order_id))
            await db.commit()

    async def insert_fill(self, f: Fill) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                (f.fill_id, f.order_id, f.market_id, f.outcome_id, f.side.value, f.price, f.size, f.fee, str(f.ts)),
            )
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
