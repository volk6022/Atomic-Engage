from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.core.config import get_settings
from app.core import logging as app_logging
from app.core import safety_config
from app.api.v1 import actions, fleet, accounts, proxies, api_credentials, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate()

    # Load ban-safety config (warmup schedules + rate limits) and, on POSIX,
    # allow `kill -HUP <pid>` to hot-reload it (FR-145). The reload endpoint
    # POST /v1/admin/reload-safety works on every platform.
    summary = safety_config.reload()
    app_logging.get_logger("startup").info(f"safety_config_loaded: {summary}")
    try:
        import signal

        if hasattr(signal, "SIGHUP"):
            def _on_sighup(*_a):
                safety_config.reload()

            signal.signal(signal.SIGHUP, _on_sighup)
    except Exception:  # noqa: BLE001 — signal handler is best-effort
        pass

    try:
        from app.db.session import get_engine
        from app.db.models import Base

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            app.state.db_connected = True
    except Exception as e:
        app.state.db_connected = False
        app_logging.get_logger("startup").warning(f"db_not_connected: {e}")

    try:
        from app.db.redis_client import get_redis

        redis = await get_redis()
        await redis.ping()
        app.state.redis_connected = True
    except Exception as e:
        app.state.redis_connected = False
        app_logging.get_logger("startup").warning(f"redis_not_connected: {e}")

    # Start the proxy health-check / failover loop (FR-008) as a background task.
    health_task = None
    if getattr(app.state, "db_connected", False) and getattr(app.state, "redis_connected", False):
        import asyncio

        from app.db.session import get_session_maker
        from app.services.proxy_manager import ProxyManager

        async def _health_loop():
            try:
                from app.db.redis_client import get_redis

                redis = await get_redis()
                async with get_session_maker()() as db:
                    await ProxyManager().run_health_check_loop(db, redis)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                app_logging.get_logger("proxy_health").warning(f"health_loop_stopped: {e}")

        health_task = asyncio.create_task(_health_loop())

    app_logging.get_logger("startup").info("application_started")

    yield

    if health_task is not None:
        health_task.cancel()
    app_logging.get_logger("shutdown").info("application_shutting_down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Telegram Fleet Orchestrator", version="2.0.0", lifespan=lifespan
    )

    app.include_router(actions.router)
    app.include_router(fleet.router)
    app.include_router(accounts.router)
    app.include_router(proxies.router)
    app.include_router(api_credentials.router)
    app.include_router(admin.router)

    return app


app = create_app()


__all__ = ["app", "create_app"]
