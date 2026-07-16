from pydantic import BaseModel, Field
from typing import Optional, Literal


class TaskCompleteEvent(BaseModel):
    event: Literal["task_complete"] = "task_complete"
    task_id: str
    account_id: int
    result: dict = Field(
        description="Result containing telegram_message_id, peer_id, etc."
    )


class TaskFailedEvent(BaseModel):
    event: Literal["task_failed"] = "task_failed"
    task_id: str
    account_id: int
    error_code: str
    retry_count: int = 0


class BanAlertEvent(BaseModel):
    event: Literal["ban_alert"] = "ban_alert"
    account_id: int
    ban_reason: str


class GeoRejectEvent(BaseModel):
    event: Literal["geo_reject"] = "geo_reject"
    account_id: int
    phone_country: str
    proxy_country: str
    risk: str


class FloodWaitEvent(BaseModel):
    event: Literal["flood_wait"] = "flood_wait"
    account_id: int
    flood_until: str
    retry_in: int


class ProxyFailSleepingEvent(BaseModel):
    event: Literal["proxy_fail_sleeping"] = "proxy_fail_sleeping"
    account_id: int
    failed_proxy_id: int
    reserve_available: bool


class ProxySwapEvent(BaseModel):
    event: Literal["proxy_swap"] = "proxy_swap"
    account_id: int
    old_proxy_id: int
    new_proxy_id: int


class WarmupTransitionEvent(BaseModel):
    event: Literal["warmup_transition"] = "warmup_transition"
    account_id: int
    from_tier: str
    to_tier: str


class WarmupCompleteEvent(BaseModel):
    event: Literal["warmup_complete"] = "warmup_complete"
    account_id: int


class IncomingMessageEvent(BaseModel):
    event: Literal["incoming_message"] = "incoming_message"
    account_id: int
    from_peer_id: int
    message: str
    message_id: int
    date: Optional[str] = None
