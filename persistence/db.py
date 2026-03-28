from __future__ import annotations

from pathlib import Path

import aiosqlite


CREATE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS orders (
      order_id TEXT PRIMARY KEY,
      market_id TEXT,
      outcome_id TEXT,
      side TEXT,
      price REAL,
      size REAL,
      filled_size REAL DEFAULT 0,
      status TEXT,
      created_at TEXT,
      updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills (
      fill_id TEXT PRIMARY KEY,
      order_id TEXT,
      market_id TEXT,
      outcome_id TEXT,
      side TEXT,
      price REAL,
      size REAL,
      fee REAL,
      ts TEXT,
      reconciled INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions_snapshots (
      ts TEXT,
      market_id TEXT,
      outcome_id TEXT,
      qty REAL,
      avg_price REAL,
      exposure REAL,
      unrealized_pnl REAL,
      realized_pnl REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pnl_snapshots (
      ts TEXT,
      equity REAL,
      pnl_today REAL,
      pnl_mtd REAL,
      progress_to_goal_500 REAL,
      mode TEXT,
      drawdown REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_state (
      key TEXT PRIMARY KEY,
      value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_metrics (
      day_key TEXT PRIMARY KEY,
      trades_count INTEGER,
      stopouts_count INTEGER,
      mispricing_trades_count INTEGER,
      pnl_day REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS selected_markets (
      market_id TEXT PRIMARY KEY,
      market_name TEXT,
      outcome_id TEXT,
      outcome_label TEXT,
      prob REAL,
      liquidity REAL,
      selected_at TEXT
    )
    """,
]


async def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        for q in CREATE_SQL:
            await db.execute(q)
        # Lightweight migration for pre-existing databases.
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = {row[1] for row in await cur.fetchall()}
        if "filled_size" not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN filled_size REAL DEFAULT 0")
        cur = await db.execute("PRAGMA table_info(fills)")
        fill_cols = {row[1] for row in await cur.fetchall()}
        if "reconciled" not in fill_cols:
            await db.execute("ALTER TABLE fills ADD COLUMN reconciled INTEGER DEFAULT 0")
        await db.commit()
