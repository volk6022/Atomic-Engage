from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPBearer

from app.core.config import get_settings
from app.db.session import get_db


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_token = HTTPBearer(auto_error=False)


async def verify_api_key(
    header: Annotated[str, Depends(api_key_header)] = None,
    bearer: Annotated[str, Depends(bearer_token)] = None,
) -> str:
    settings = get_settings()

    key = header or (bearer.credentials if bearer else None)

    if not key or key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )

    return key


async def get_db_dep():
    async for session in get_db():
        yield session


async def get_redis_dep():
    from app.db.redis_client import get_redis

    return await get_redis()


VerifyAPIKey = Annotated[str, Depends(verify_api_key)]
GetDB = Annotated[object, Depends(get_db_dep)]
GetRedis = Annotated[object, Depends(get_redis_dep)]
