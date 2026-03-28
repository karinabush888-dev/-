from __future__ import annotations

from time import monotonic

import httpx


class TelegramNotifier:
    def __init__(self, enabled: bool, token: str, chat_id: str) -> None:
        self.enabled = enabled
        self.token = token
        self.chat_id = chat_id
        self._dedupe_cache: dict[str, float] = {}

    async def send(self, text: str, *, dedupe_key: str | None = None, dedupe_ttl_sec: int = 120) -> None:
        if not self.enabled:
            return
        if not self.token or not self.chat_id:
            return
        if dedupe_key:
            now = monotonic()
            expiry = self._dedupe_cache.get(dedupe_key)
            if expiry and expiry > now:
                return
            self._dedupe_cache[dedupe_key] = now + max(1, dedupe_ttl_sec)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": self.chat_id, "text": text[:3900]})
