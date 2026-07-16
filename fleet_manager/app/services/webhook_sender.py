import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx



WEBHOOK_BACKOFF = [30, 60, 120, 240, 480]
MAX_ATTEMPTS = 5


@dataclass
class WebhookDeliveryRecord:
    id: int
    url: str
    payload: dict
    attempts: int = 0
    status: str = "pending"


class WebhookSender:
    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self.client = http_client or httpx.AsyncClient(timeout=10.0)

    async def send(self, delivery_id: int, url: str, payload: dict) -> bool:
        logger = logging.getLogger(f"webhook.{delivery_id}")

        for attempt_num, delay in enumerate(WEBHOOK_BACKOFF, 1):
            try:
                response = await self.client.post(url, json=payload)

                if response.status_code < 400:
                    return True

            except httpx.RequestError as e:
                logger.warning(f"attempt_{attempt_num}_error: {e}")

            if attempt_num < len(WEBHOOK_BACKOFF):
                await self._sleep(delay)

        logger.error(
            f"exhausted_after_{MAX_ATTEMPTS}_attempts payload={json.dumps(payload)}"
        )
        return False

    async def _sleep(self, seconds: int) -> None:
        import asyncio

        await asyncio.sleep(seconds)
