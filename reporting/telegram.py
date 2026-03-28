from __future__ import annotations

import httpx


class TelegramNotifier:
    def __init__(self, enabled: bool, token: str, chat_id: str) -> None:
        self.enabled = enabled
        self.token = token
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        if not self.token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": self.chat_id, "text": text[:3900]})
