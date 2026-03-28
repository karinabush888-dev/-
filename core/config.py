from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from core.types import BotMode


class EnvConfig(BaseModel):
    mode: BotMode = BotMode.PAPER
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    db_path: str = "/app/data/bot.sqlite3"
    log_level: str = "INFO"
    cancel_all_on_exit: bool = True
    dry_run: bool = False
    paper_fill_model: str = "touch_probability"
    paper_latency_ms: int = 250
    starting_equity: float = 100.0
    polymarket_api_base: str = "https://clob.polymarket.com"
    polymarket_private_key: str = ""
    polymarket_proxy_address: str = ""
    polymarket_funder: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    refresh_sec: int = 15
    http_timeout_sec: int = 15
    max_retries: int = 5
    retry_backoff_min: float = 0.5
    retry_backoff_max: float = 8.0


class MarketsConfig(BaseModel):
    markets: list[dict[str, str]]
    selection: dict[str, Any]


class RiskConfig(BaseModel):
    daily_loss_limit_pct: float
    max_stopouts_per_day: int
    max_open_markets: int
    max_mispricing_trades_per_day: int
    pause_before_resolution_minutes: int
    mm_allocation_pct: float
    mis_allocation_pct: float
    order_size_mm_pct: float
    order_size_mis_pct: float
    order_size_mm_min: float
    order_size_mm_max: float
    order_size_mis_min: float
    order_size_mis_max: float
    max_exposure_per_outcome_pct: float
    mm_inventory_skew_trigger_pct: float
    mm_reduce_only_trigger_pct: float
    mis_tp1_pct: float
    mis_tp1_close_pct: float
    mis_tp2_pct: float
    mis_tp2_close_pct: float
    mis_stop_pct: float
    mis_time_stop_minutes: int
    accel_3d_pnl_threshold_pct: float
    accel_mm_size_multiplier: float
    brake_size_multiplier: float
    adaptation_window_hours: int


class Settings(BaseModel):
    env: EnvConfig
    markets: MarketsConfig
    risk: RiskConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Settings:
    load_dotenv()
    env = EnvConfig(
        mode=BotMode(os.getenv("MODE", "PAPER").upper()),
        telegram_enabled=os.getenv("TELEGRAM_ENABLED", "true").lower() == "true",
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        db_path=os.getenv("DB_PATH", "/app/data/bot.sqlite3"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        cancel_all_on_exit=os.getenv("CANCEL_ALL_ON_EXIT", "true").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        paper_fill_model=os.getenv("PAPER_FILL_MODEL", "touch_probability"),
        paper_latency_ms=int(os.getenv("PAPER_LATENCY_MS", "250")),
        starting_equity=float(os.getenv("STARTING_EQUITY", "100")),
        polymarket_api_base=os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com"),
        polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        polymarket_proxy_address=os.getenv("POLYMARKET_PROXY_ADDRESS", ""),
        polymarket_funder=os.getenv("POLYMARKET_FUNDER", ""),
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
        polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
        refresh_sec=int(os.getenv("REFRESH_SEC", "15")),
        http_timeout_sec=int(os.getenv("HTTP_TIMEOUT_SEC", "15")),
        max_retries=int(os.getenv("MAX_RETRIES", "5")),
        retry_backoff_min=float(os.getenv("RETRY_BACKOFF_MIN", "0.5")),
        retry_backoff_max=float(os.getenv("RETRY_BACKOFF_MAX", "8")),
    )
    markets_path = Path("config/markets.yaml")
    risk_path = Path("config/risk.yaml")
    markets = MarketsConfig(**_load_yaml(markets_path))
    risk = RiskConfig(**_load_yaml(risk_path))
    settings = Settings(env=env, markets=markets, risk=risk)
    if settings.env.telegram_enabled and (not settings.env.telegram_bot_token or not settings.env.telegram_chat_id):
        raise ValueError("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if settings.env.mode == BotMode.LIVE:
        missing = [
            name
            for name, value in {
                "POLYMARKET_API_KEY": settings.env.polymarket_api_key,
                "POLYMARKET_API_SECRET": settings.env.polymarket_api_secret,
                "POLYMARKET_PASSPHRASE": settings.env.polymarket_passphrase,
                "POLYMARKET_PRIVATE_KEY": settings.env.polymarket_private_key,
                "POLYMARKET_PROXY_ADDRESS": settings.env.polymarket_proxy_address,
                "POLYMARKET_FUNDER": settings.env.polymarket_funder,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"LIVE mode requires credentials: {', '.join(missing)}")
    return settings
