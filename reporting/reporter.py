from __future__ import annotations


class Reporter:
    def __init__(self, notifier) -> None:
        self.notifier = notifier

    async def hourly(self, payload: dict) -> None:
        txt = (
            f"hourly report\n"
            f"equity_now={payload['equity_now']:.2f}\n"
            f"pnl_today={payload['pnl_today']:.2f}\n"
            f"pnl_mtd={payload['pnl_mtd']:.2f}\n"
            f"progress_to_goal_500={payload['progress_to_goal_500']:.2f}%\n"
            f"open_markets={payload['open_markets']}\n"
            f"stopouts_today={payload['stopouts_today']}\n"
            f"trades_today={payload['trades_today']}\n"
            f"mispricing_trades_today={payload['mispricing_trades_today']}\n"
            f"mode={payload['mode']}\n"
            f"positions={payload['positions']}"
        )
        await self.notifier.send(txt)

    async def daily(self, payload: dict) -> None:
        txt = (
            f"daily summary\n"
            f"equity_start={payload['equity_start']:.2f}\n"
            f"equity_end={payload['equity_end']:.2f}\n"
            f"pnl_day={payload['pnl_day']:.2f}\n"
            f"pnl_mtd={payload['pnl_mtd']:.2f}\n"
            f"progress_to_500={payload['progress']:.2f}%\n"
            f"stopouts={payload['stopouts']}\n"
            f"trades={payload['trades']}\n"
            f"mispricing_trades={payload['mis_trades']}\n"
            f"max_drawdown={payload['max_drawdown']:.4f}\n"
            f"mode_close={payload['mode']}"
        )
        await self.notifier.send(txt)
