"""api_id pool registration (FR-003 pool; gateway-api.md §11)."""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api import deps
from app.db.models import ApiCredential

router = APIRouter(prefix="/v1/api-credentials", tags=["api_credentials"])

MAX_CREDENTIALS = 100


class ApiCredentialRequest(BaseModel):
    api_id: int
    api_hash: str


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_api_credential(
    request: ApiCredentialRequest, db: deps.GetDB, api_key: deps.VerifyAPIKey
):
    existing = (
        await db.execute(select(ApiCredential).where(ApiCredential.api_id == request.api_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"api_id {request.api_id} is already registered",
        )

    count = (await db.execute(select(func.count()).select_from(ApiCredential))).scalar_one()
    if count >= MAX_CREDENTIALS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"api_id pool limit reached ({MAX_CREDENTIALS} credentials max)",
        )

    cred = ApiCredential(api_id=request.api_id, api_hash=request.api_hash, account_count=0)
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return {"credential_id": cred.id, "api_id": cred.api_id, "account_count": cred.account_count}


@router.get("/{credential_id}")
async def get_api_credential(credential_id: int, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    cred = (
        await db.execute(select(ApiCredential).where(ApiCredential.id == credential_id))
    ).scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    return {"id": cred.id, "api_id": cred.api_id, "account_count": cred.account_count}
