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

    async def send_once(self, url: str, payload: dict) -> tuple[bool, str]:
        """One POST, no retry, no sleeping. Returns (delivered, detail).

        The retry schedule lives in the `deliver_webhook` worker instead of inside this
        call. `send` below keeps the old inline-retry behaviour for callers that have no
        queue, but nothing on the task path may use it: its backoff sums to 450 s and it
        is awaited inside the job, so a single unreachable receiver used to hold a worker
        slot for seven and a half minutes -- observed in production against a
        `N8N_SYSTEM_WEBHOOK_URL` that did not resolve.
        """
        try:
            response = await self.client.post(url, json=payload)
        except httpx.RequestError as e:
            return False, f"{type(e).__name__}: {e}"
        if response.status_code < 400:
            return True, str(response.status_code)
        return False, f"HTTP {response.status_code}"

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
