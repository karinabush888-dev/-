from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

from app.bootstrap import build_context


async def _main() -> None:
    ctx = await build_context()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _stop)

    task = asyncio.create_task(ctx.scheduler.run())
    await stop_event.wait()
    await ctx.scheduler.shutdown()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


if __name__ == "__main__":
    asyncio.run(_main())
