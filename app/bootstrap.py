from __future__ import annotations

from dataclasses import dataclass

from core.config import Settings, load_settings
from core.logging_setup import setup_logging
from core.state import RuntimeState
from exchange.market_selector import select_outcome
from exchange.paper import PaperExchangeClient
from exchange.polymarket import LivePolymarketClient
from persistence.db import init_db
from persistence.repository import Repository
from reporting.reporter import Reporter
from reporting.telegram import TelegramNotifier
from risk.engine import RiskEngine
from services.execution import ExecutionManager
from services.pnl import PnLEngine
from services.scheduler import Scheduler
from strategies.mispricing import MispricingStrategy
from strategies.mm import MarketMakingStrategy


@dataclass
class AppContext:
    settings: Settings
    exchange: object
    repo: Repository
    notifier: TelegramNotifier
    reporter: Reporter
    risk_engine: RiskEngine
    pnl_engine: PnLEngine
    mm: MarketMakingStrategy
    mis: MispricingStrategy
    exec: ExecutionManager
    scheduler: Scheduler
    state: RuntimeState
    selector: object
    pnl_state: dict

    def mode_multipliers(self) -> tuple[float, float]:
        m = self.state.stats.mode.value
        if m == "ACCEL":
            return self.settings.risk.accel_mm_size_multiplier, 1.0
        if m == "BRAKE":
            return self.settings.risk.brake_size_multiplier, self.settings.risk.brake_size_multiplier
        return 1.0, 1.0


async def build_context() -> AppContext:
    settings = load_settings()
    setup_logging(settings.env.log_level)
    await init_db(settings.env.db_path)

    if settings.env.mode.value == "PAPER":
        exchange = PaperExchangeClient(starting_equity=settings.env.starting_equity, latency_ms=settings.env.paper_latency_ms)
    else:
        exchange = LivePolymarketClient(
            api_base=settings.env.polymarket_api_base,
            api_key=settings.env.polymarket_api_key,
            api_secret=settings.env.polymarket_api_secret,
            passphrase=settings.env.polymarket_passphrase,
            timeout_sec=settings.env.http_timeout_sec,
        )

    repo = Repository(settings.env.db_path)
    notifier = TelegramNotifier(settings.env.telegram_enabled, settings.env.telegram_bot_token, settings.env.telegram_chat_id)
    reporter = Reporter(notifier)
    risk_engine = RiskEngine(settings.risk)
    pnl_engine = PnLEngine(settings.env.starting_equity)
    mm = MarketMakingStrategy(settings.risk)
    mis = MispricingStrategy(settings.risk)
    exec_manager = ExecutionManager(exchange, repo, notifier)
    state = RuntimeState()

    ctx = AppContext(
        settings=settings,
        exchange=exchange,
        repo=repo,
        notifier=notifier,
        reporter=reporter,
        risk_engine=risk_engine,
        pnl_engine=pnl_engine,
        mm=mm,
        mis=mis,
        exec=exec_manager,
        scheduler=None,  # type: ignore[arg-type]
        state=state,
        selector=select_outcome,
        pnl_state={"equity": settings.env.starting_equity, "pnl_today": 0.0, "pnl_mtd": 0.0, "progress": settings.env.starting_equity / 500 * 100, "drawdown": 0.0},
    )
    ctx.scheduler = Scheduler(ctx)
    return ctx
