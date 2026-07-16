"""Reserve proxy pool endpoints (FR-026/FR-106)."""
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api import deps
from app.db.models import Proxy
from app.services.proxy_manager import ProxyManager

router = APIRouter(prefix="/v1/proxies", tags=["proxies"])


class ProxyCreateRequest(BaseModel):
    url: str
    proxy_type: Literal["mobile_4g", "residential", "datacenter"] = "residential"
    country: Optional[str] = None
    tz_offset: Optional[int] = None


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_proxy(request: ProxyCreateRequest, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    pm = ProxyManager()
    country = pm.resolve_country(request.url, request.country)
    if not country or country == "XX":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cannot determine proxy country (no GeoIP and no hint); pass country",
        )
    host, _port, _u, _pw = pm.parse_proxy_url(request.url)

    proxy = Proxy(
        url=request.url,
        proxy_type=request.proxy_type,
        country=country,
        tz_offset=request.tz_offset or 0,
        state="reserve",
        is_healthy=True,
    )
    db.add(proxy)
    await db.commit()
    await db.refresh(proxy)
    return {
        "proxy_id": proxy.id,
        "host": host,
        "country": proxy.country,
        "proxy_type": proxy.proxy_type,
        "state": proxy.state,
        "is_healthy": proxy.is_healthy,
    }


@router.get("/{proxy_id}")
async def get_proxy(proxy_id: int, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    proxy = (await db.execute(select(Proxy).where(Proxy.id == proxy_id))).scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxy not found")
    return {
        "proxy_id": proxy.id,
        "country": proxy.country,
        "proxy_type": proxy.proxy_type,
        "state": proxy.state,
        "is_healthy": proxy.is_healthy,
    }
