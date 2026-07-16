"""Account lifecycle endpoints (US2 / FR-106/107/146).

Onboarding imports an EXISTING Telegram session: it preserves the session's original
device fingerprint and api_id/api_hash when supplied (FR-146) and only generates an
Android fingerprint as a fallback. Geo coherence (phone country vs proxy exit country)
is enforced before any row is persisted; the whole operation is one transaction so a
geo-mismatch creates nothing.
"""
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api import deps
from app.core.clock import get_clock
from app.core.constants import AccountStatus, UseCase, WarmupTier
from app.db.models import Account, ApiCredential, Proxy
from app.services import telemetry
from app.services.fingerprint import DeviceFingerprintGenerator
from app.services.geo_match import GeoMatchValidator, RiskLevel
from app.services.proxy_manager import ProxyManager

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])

MAX_ACCOUNTS_PER_API_ID = 200


class FingerprintModel(BaseModel):
    device_model: str
    system_version: str
    app_version: str
    lang_code: str = "en"
    system_lang_code: str = "en-US"


class OnboardRequest(BaseModel):
    phone: str
    session_string: str
    proxy_url: str
    use_case: Literal["reactions", "join_groups", "cold_dm", "inviting"]
    proxy_country: Optional[str] = Field(default=None, description="ISO-3166 alpha-2; else GeoIP/login hint")
    proxy_type: Literal["mobile_4g", "residential", "datacenter"] = "residential"
    tz_offset: Optional[int] = None
    work_start: int = 8
    work_end: int = 22
    cohort: Optional[str] = Field(default=None, description="experiment cohort label (FR-143)")
    # Imported-session identity (FR-146) — preserve original fingerprint + api_id.
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    fingerprint: Optional[FingerprintModel] = None


class ProxyChangeRequest(BaseModel):
    proxy_url: str
    proxy_country: Optional[str] = None
    proxy_type: Literal["mobile_4g", "residential", "datacenter"] = "residential"
    tz_offset: Optional[int] = None


def _norm_phone(phone: str) -> str:
    return phone if phone.startswith("+") else f"+{phone}"


async def _select_api_credential(db, request: OnboardRequest) -> ApiCredential:
    """Find-or-create the api credential, preserving an imported api_id (FR-146)."""
    if request.api_id is not None:
        cred = (
            await db.execute(select(ApiCredential).where(ApiCredential.api_id == request.api_id))
        ).scalar_one_or_none()
        if cred is None:
            cred = ApiCredential(
                api_id=request.api_id, api_hash=request.api_hash or "", account_count=0
            )
            db.add(cred)
            await db.flush()
        if cred.account_count >= MAX_ACCOUNTS_PER_API_ID:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"api_id {cred.api_id} at capacity ({MAX_ACCOUNTS_PER_API_ID})",
            )
        return cred

    cred = (
        await db.execute(
            select(ApiCredential)
            .where(ApiCredential.account_count < MAX_ACCOUNTS_PER_API_ID)
            .order_by(ApiCredential.account_count)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no api credential with free capacity; register one or pass api_id",
        )
    return cred


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_account(request: OnboardRequest, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    phone = _norm_phone(request.phone)
    phone_country = GeoMatchValidator().extract_phone_country(phone)
    if not phone_country:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"could not determine phone country from {phone}",
        )

    pm = ProxyManager()
    proxy_country = pm.resolve_country(request.proxy_url, request.proxy_country)
    if not proxy_country or proxy_country == "XX":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cannot determine proxy country (no GeoIP and no hint); pass proxy_country",
        )
    if request.proxy_type == "datacenter":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="datacenter proxies are not allowed for account-facing operations",
        )

    geo = GeoMatchValidator().validate(phone_country=phone_country, proxy_country=proxy_country)
    if geo.risk == RiskLevel.CRITICAL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"geo_mismatch: phone {phone_country} != proxy {proxy_country}. "
                "Account not created."
            ),
        )

    cred = await _select_api_credential(db, request)

    if request.fingerprint is not None:  # preserve imported identity (FR-146)
        fp = request.fingerprint
    else:
        gen = DeviceFingerprintGenerator().generate()
        fp = FingerprintModel(
            device_model=gen.device_model,
            system_version=gen.system_version,
            app_version=gen.app_version,
            lang_code=gen.lang_code,
            system_lang_code=gen.system_lang_code,
        )

    proxy = Proxy(
        url=request.proxy_url,
        proxy_type=request.proxy_type,
        country=proxy_country,
        tz_offset=request.tz_offset or 0,
        state="assigned",
        is_healthy=True,
    )
    db.add(proxy)
    await db.flush()

    account = Account(
        phone=phone,
        phone_country=phone_country,
        session_string=request.session_string,
        api_credential_id=cred.id,
        proxy_id=proxy.id,
        device_model=fp.device_model,
        system_version=fp.system_version,
        app_version=fp.app_version,
        lang_code=fp.lang_code,
        system_lang_code=fp.system_lang_code,
        use_case=request.use_case,
        status=AccountStatus.WARMUP,
        warmup_tier=WarmupTier.FRESH,
        warmup_day=0,
        work_start=request.work_start,
        work_end=request.work_end,
        cohort=request.cohort,
        first_seen_at=get_clock().now(),   # survival window opens here (FR-143)
    )
    db.add(account)
    cred.account_count += 1
    await db.flush()
    account_id = account.id
    await telemetry.record_for_account(
        db, account, event_type=telemetry.ONBOARDED, outcome="ok",
        warmup_params={"use_case": account.use_case, "warmup_tier": account.warmup_tier},
    )
    await db.commit()

    return {
        "account_id": account_id,
        "phone_country": phone_country,
        "proxy_country": proxy_country,
        "geo_status": "OK",
        "device_fingerprint": {
            "device_model": fp.device_model,
            "system_version": fp.system_version,
            "app_version": fp.app_version,
        },
        "warmup_tier": WarmupTier.FRESH,
        "status": AccountStatus.WARMUP,
    }


@router.get("/{account_id}")
async def get_account(account_id: int, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    account = (
        await db.execute(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
        )
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    return {
        "account_id": account.id,
        "phone": account.phone,
        "phone_country": account.phone_country,
        "status": account.status,
        "warmup_tier": account.warmup_tier,
        "use_case": account.use_case,
        "warmup_day": account.warmup_day,
        "proxy": {
            "id": account.proxy.id,
            "country": account.proxy.country,
            "type": account.proxy.proxy_type,
            "is_healthy": account.proxy.is_healthy,
        }
        if account.proxy
        else None,
    }


@router.put("/{account_id}/proxy")
async def reassign_proxy(
    account_id: int, request: ProxyChangeRequest, db: deps.GetDB, api_key: deps.VerifyAPIKey
):
    account = (
        await db.execute(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
        )
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    pm = ProxyManager()
    proxy_country = pm.resolve_country(request.proxy_url, request.proxy_country)
    if not proxy_country or proxy_country == "XX":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="cannot determine proxy country; pass proxy_country")
    geo = GeoMatchValidator().validate(phone_country=account.phone_country, proxy_country=proxy_country)
    if geo.risk == RiskLevel.CRITICAL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"geo_mismatch: proxy {proxy_country} != account phone {account.phone_country}",
        )

    if account.proxy:
        account.proxy.state = "reserve"
    new_proxy = Proxy(
        url=request.proxy_url, proxy_type=request.proxy_type, country=proxy_country,
        tz_offset=request.tz_offset or 0, state="assigned", is_healthy=True,
    )
    db.add(new_proxy)
    await db.flush()
    account.proxy_id = new_proxy.id
    await db.commit()
    return {"account_id": account.id, "proxy_country": proxy_country, "geo_status": "OK", "status": account.status}


@router.post("/{account_id}/reactivate")
async def reactivate_account(
    account_id: int, request: ProxyChangeRequest, db: deps.GetDB, api_key: deps.VerifyAPIKey
):
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if account.status != AccountStatus.SLEEPING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"account is {account.status}, not sleeping")

    pm = ProxyManager()
    proxy_country = pm.resolve_country(request.proxy_url, request.proxy_country)
    geo = GeoMatchValidator().validate(phone_country=account.phone_country, proxy_country=proxy_country or "XX")
    if geo.risk == RiskLevel.CRITICAL or not proxy_country:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="geo_mismatch or unknown proxy country")

    healthy = await pm.health_check(request.proxy_url)
    if not healthy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proxy_unhealthy: provided proxy failed health check. Account remains sleeping.",
        )

    new_proxy = Proxy(
        url=request.proxy_url, proxy_type=request.proxy_type, country=proxy_country,
        tz_offset=request.tz_offset or 0, state="assigned", is_healthy=True,
    )
    db.add(new_proxy)
    await db.flush()
    account.proxy_id = new_proxy.id
    account.status = AccountStatus.ACTIVE
    await db.commit()
    return {"account_id": account.id, "status": AccountStatus.ACTIVE, "proxy_country": proxy_country, "geo_status": "OK"}


@router.post("/{account_id}/unban")
async def unban_account(account_id: int, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if account.status != AccountStatus.BANNED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"account is {account.status}, not banned")

    previous = account.ban_reason
    account.status = AccountStatus.ACTIVE
    account.ban_reason = None
    await db.commit()
    return {"account_id": account.id, "status": AccountStatus.ACTIVE, "previous_ban_reason": previous}
