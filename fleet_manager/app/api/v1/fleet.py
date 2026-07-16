from fastapi import APIRouter, Response

from app.api import deps


router = APIRouter(prefix="/v1/fleet", tags=["fleet"])


@router.get("/status")
async def get_fleet_status(db: deps.GetDB, api_key: deps.VerifyAPIKey):
    from sqlalchemy import select, func
    from app.db.models import Account

    stmt = select(Account.status, func.count()).group_by(Account.status)
    result = await db.execute(stmt)
    rows = result.all()

    counts = {status: count for status, count in rows}

    return {"accounts": counts}


@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.get("/metrics")
async def get_metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
