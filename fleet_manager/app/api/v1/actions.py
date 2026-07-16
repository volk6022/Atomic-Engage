import uuid
from pydantic import BaseModel, Field
from typing import Optional
from fastapi import APIRouter, HTTPException, status

from app.api import deps
from app.core.constants import AccountStatus, READ_ACTIONS, TaskStatus
from app.db.models import Account, Task
from app.services.geo_match import GeoMatchValidator, RiskLevel


router = APIRouter(prefix="/v1", tags=["actions"])


class SendMessagePayload(BaseModel):
    peer_id: int = Field(..., description="Telegram peer ID to send message to")
    text: str = Field(..., min_length=1, max_length=4096)
    reply_to_message_id: Optional[int] = None


class JoinGroupPayload(BaseModel):
    invite_link: str = Field(..., description="Telegram invite link")


class ReactPayload(BaseModel):
    peer_id: int
    message_id: int
    reaction: str = "👍"


class ResolveUsernamePayload(BaseModel):
    username: str


class InviteToGroupPayload(BaseModel):
    group_username: str
    user_peer_id: int


class GetChatInfoPayload(BaseModel):
    """Read-only public profile lookup (§3.2). Exactly one of username|peer_id."""
    username: Optional[str] = None
    peer_id: Optional[int] = None
    with_pinned: bool = True


class GetChatHistoryPayload(BaseModel):
    """Read-only last-N public posts (§3.3). `limit` hard-capped at 50."""
    username: Optional[str] = None
    peer_id: Optional[int] = None
    limit: int = Field(default=30, ge=1, le=50)
    min_date: Optional[str] = None
    min_id: Optional[int] = None
    offset_id: Optional[int] = None


class ActionRequest(BaseModel):
    account_id: int = Field(..., gt=0)
    action: str = Field(
        ...,
        description=(
            "Action type: send_message, join_group, react, resolve_username, "
            "invite_to_group, get_chat_info, get_chat_history. The last three are "
            "read-only research lookups (warmup-exempt, read-budget limited)."
        ),
    )
    payload: dict = Field(..., description="Action-specific payload")
    webhook_url: str
    priority: int = Field(default=5, ge=1, le=10)


class ActionResponse(BaseModel):
    task_id: str
    status: str = "queued"
    account_id: int


@router.post(
    "/action", response_model=ActionResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_action(
    request: ActionRequest,
    db: deps.GetDB,
    api_key: deps.VerifyAPIKey,
):
    if request.action not in [
        "send_message",
        "join_group",
        "react",
        "resolve_username",
        "invite_to_group",
        "get_chat_info",
        "get_chat_history",
    ]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid action type",
        )

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = select(Account).options(selectinload(Account.proxy)).where(Account.id == request.account_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )

    if account.status in [AccountStatus.BANNED, AccountStatus.SLEEPING]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account status is {account.status}",
        )

    validator = GeoMatchValidator()
    geo_result = validator.validate(
        phone_country=account.phone_country,
        proxy_country=account.proxy.country if account.proxy else "XX",
    )

    if geo_result.risk == RiskLevel.CRITICAL:
        account.status = AccountStatus.SLEEPING
        await db.commit()

        from app.services.webhook_sender import WebhookSender
        from app.core.config import get_settings

        settings = get_settings()

        await WebhookSender().send(
            delivery_id=0,
            url=settings.N8N_SYSTEM_WEBHOOK_URL,
            payload={
                "event": "geo_reject",
                "account_id": account.id,
                "phone_country": account.phone_country,
                "proxy_country": account.proxy.country if account.proxy else "XX",
                "risk": geo_result.risk,
            },
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Geographic mismatch: account set to sleeping",
        )

    # Warmup gate (FR-110 / G4): an account may only perform actions its current
    # warmup tier permits — a `fresh` account cannot send cold messages until it
    # has warmed up to the tier that unlocks the action (see config/safety.yaml,
    # hot-reloadable). READ_ACTIONS (resolve_username + the get_chat_* research
    # lookups) are infra/read-only with no behavioural footprint, so they are exempt
    # from the warmup gate (they remain read-budget limited at the worker — §4.1).
    if request.action not in READ_ACTIONS:
        from app.services.warmup import WarmupPipeline

        allowed = WarmupPipeline().get_allowed_actions(account)
        if request.action not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Account not warmed for '{request.action}' "
                    f"(use_case={account.use_case}, warmup_tier={account.warmup_tier}). "
                    f"Allowed now: {allowed}."
                ),
            )

    task_external_id = str(uuid.uuid4())
    task = Task(
        external_id=task_external_id,
        account_id=request.account_id,
        task_type=request.action,
        payload=request.payload,
        status=TaskStatus.QUEUED,
        webhook_url=request.webhook_url,
        priority=request.priority,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    from arq import create_pool
    from arq.connections import RedisSettings
    from app.core.config import get_settings

    settings = get_settings()

    pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    await pool.enqueue_job(request.action, task_id=task.id)

    return ActionResponse(
        task_id=task_external_id, status="queued", account_id=request.account_id
    )
