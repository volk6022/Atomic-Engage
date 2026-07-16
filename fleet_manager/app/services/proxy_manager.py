import asyncio
import logging
from typing import Optional

from app.services.geo_match import GeoMatchValidator
from app.core.config import get_settings


logger = logging.getLogger(__name__)


class ProxyManager:
    def __init__(self):
        settings = get_settings()
        self.validator = GeoMatchValidator(
            getattr(settings, "GEOIP_CITY_DB_PATH", "") or "",
            getattr(settings, "GEOIP_ASN_DB_PATH", "") or "",
        )

    def country_from_login_hint(self, url: str) -> Optional[str]:
        """Some residential providers encode the exit country in the proxy login,
        e.g. puls-proxy `<login>__cr.us`. Returns an ISO-3166 alpha-2 or None."""
        _h, _p, username, _pw = self.parse_proxy_url(url)
        if username and "__cr." in username:
            tail = username.split("__cr.", 1)[1]
            # the country is the leading token; puls appends params after it with
            # either '.' or ';' (e.g. "ru;sessttl.10"), so split on both.
            cc = tail.replace("_", ".").replace(";", ".").split(".")[0]
            if len(cc) == 2 and cc.isalpha():
                return cc.upper()
        return None

    def resolve_country(self, url: str, explicit: Optional[str] = None) -> Optional[str]:
        """Determine the proxy exit country: explicit > GeoIP (if mmdb) > login hint."""
        if explicit:
            return explicit.upper()
        host, _p, _u, _pw = self.parse_proxy_url(url)
        country, _asn, _tz = self.validator.get_proxy_info(host)
        if country and country != "XX":
            return country
        return self.country_from_login_hint(url)

    def parse_proxy_url(
        self, url: str
    ) -> tuple[str, int, Optional[str], Optional[str]]:
        """Parse socks5://user:pass@host:port (or http/https) robustly."""
        from urllib.parse import urlparse, unquote

        u = urlparse(url)
        if u.hostname:
            host = u.hostname
            port = u.port or (1080 if (u.scheme or "").startswith("socks") else 8080)
            username = unquote(u.username) if u.username else None
            password = unquote(u.password) if u.password else None
            return host, port, username, password

        # Fallback for scheme-less "host:port"
        host_port = url.split("/")[0]
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            return host, int(port), None, None
        return host_port, 8080, None, None

    async def geo_validate(
        self, url: str, phone_country: str, db
    ) -> tuple[GeoMatchValidator, str]:
        host, port, username, password = self.parse_proxy_url(url)

        country, asn_org, tz_offset = self.validator.get_proxy_info(host)

        result = self.validator.validate(
            phone_country=phone_country, proxy_country=country, asn_org=asn_org
        )

        return result, country

    async def health_check(self, url: str, timeout: float = 10.0) -> bool:
        host, port, _, _ = self.parse_proxy_url(url)

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def assign_reserve(self, account, db, redis_conn) -> Optional[object]:
        from sqlalchemy import select
        from app.db.models import Proxy

        if not hasattr(account, "phone_country"):
            return None

        phone_country = account.phone_country

        stmt = (
            select(Proxy)
            .where(
                Proxy.state == "reserve",
                Proxy.country == phone_country,
                Proxy.is_healthy.is_(True),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        result = await db.execute(stmt)
        proxy = result.scalar_one_or_none()

        if proxy:
            proxy.state = "assigned"
            await db.commit()
            return proxy

        return None

    async def run_health_check_loop(self, db, redis_conn):
        from sqlalchemy import select
        from app.db.models import Proxy, Account
        from app.db.redis_client import proxy_health_set

        while True:
            await asyncio.sleep(300)

            stmt = select(Proxy).where(Proxy.state.in_(["assigned", "reserve"]))
            result = await db.execute(stmt)
            proxies = result.scalars().all()

            for proxy in proxies:
                is_healthy = await self.health_check(proxy.url)
                await proxy_health_set(redis_conn, proxy.id, is_healthy)

                if not is_healthy:
                    stmt = select(Account).where(Account.proxy_id == proxy.id)
                    result = await db.execute(stmt)
                    account = result.scalar_one_or_none()

                    if account and account.status == "active":
                        account.status = "sleeping"
                        await db.commit()

                        settings = get_settings()

                        from app.services.webhook_sender import WebhookSender

                        await WebhookSender().send(
                            delivery_id=0,
                            url=settings.N8N_SYSTEM_WEBHOOK_URL,
                            payload={
                                "event": "proxy_fail_sleeping",
                                "account_id": account.id,
                                "failed_proxy_id": proxy.id,
                                "reserve_available": False,
                            },
                        )

            logger.info(f"health_check_cycle proxies={len(proxies)}")
