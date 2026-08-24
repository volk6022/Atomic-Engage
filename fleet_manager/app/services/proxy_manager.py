import asyncio
import logging
from typing import Optional

from app.services.geo_match import GeoMatchValidator
from app.core.config import get_settings


logger = logging.getLogger(__name__)

# Consecutive failed checks before a proxy is believed dead. The loop runs every 5
# minutes with a 10s timeout, so three strikes is a quarter hour of unreachability
# rather than one connection that happened not to open.
PROXY_FAIL_STRIKES = 3


class ProxyManager:
    def __init__(self):
        settings = get_settings()
        self.validator = GeoMatchValidator(
            getattr(settings, "GEOIP_CITY_DB_PATH", "") or "",
            getattr(settings, "GEOIP_ASN_DB_PATH", "") or "",
        )

    def country_from_login_hint(self, url: str) -> Optional[str]:
        """Some residential providers encode the exit country in the proxy login,
        e.g. our proxy provider's `<login>__cr.us`. Returns an ISO-3166 alpha-2 or None."""
        _h, _p, username, _pw = self.parse_proxy_url(url)
        if username and "__cr." in username:
            tail = username.split("__cr.", 1)[1]
            # the country is the leading token; the provider appends params after it
            # with either '.' or ';' (e.g. "ru;sessttl.10"), so split on both.
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

    def rotate_port(
        self,
        url: str,
        lo: Optional[int] = None,
        hi: Optional[int] = None,
        exclude: Optional[set[int]] = None,
    ) -> str:
        """Return `url` with its trailing `:port` swapped for a different port in
        [lo, hi]. Only the port changes — the host/login (which carries the exit
        country, e.g. `__cr.kz`) is preserved, so geo coherence is unaffected. Returns
        the original url unchanged if it has no numeric port or no alternative exists.
        """
        import random

        settings = get_settings()
        lo = settings.PROXY_STICKY_PORT_MIN if lo is None else lo
        hi = settings.PROXY_STICKY_PORT_MAX if hi is None else hi

        base, sep, cur = url.rpartition(":")
        if not sep:
            return url
        try:
            cur_port = int(cur)
        except ValueError:
            return url

        skip = set(exclude or ())
        skip.add(cur_port)
        choices = [p for p in range(lo, hi + 1) if p not in skip]
        if not choices:  # everything excluded — allow any port but the current one
            choices = [p for p in range(lo, hi + 1) if p != cur_port]
        if not choices:
            return url
        return f"{base}:{random.choice(choices)}"

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

        from app.core import logging as app_logging
        from app.services import telemetry

        log = app_logging.get_logger("proxy_health")

        # Consecutive failures per proxy id. A proxy is only believed dead after
        # PROXY_FAIL_STRIKES checks in a row.
        strikes: dict[int, int] = {}

        while True:
            await asyncio.sleep(300)

            stmt = select(Proxy).where(Proxy.state.in_(["assigned", "reserve"]))
            result = await db.execute(stmt)
            proxies = result.scalars().all()

            slept = 0
            for proxy in proxies:
                is_healthy = await self.health_check(proxy.url)
                await proxy_health_set(redis_conn, proxy.id, is_healthy)

                if is_healthy:
                    strikes.pop(proxy.id, None)
                    continue

                strikes[proxy.id] = strikes.get(proxy.id, 0) + 1
                n = strikes[proxy.id]
                if n < PROXY_FAIL_STRIKES:
                    # A single timeout is a blip, not an outage. Sleeping on the first
                    # one is how five accounts went down for five days on a proxy that
                    # was reachable the whole time either side of the check.
                    log.warning("proxy_check_failed", proxy_id=proxy.id, strike=n,
                                of=PROXY_FAIL_STRIKES)
                    continue

                # Not scalar_one_or_none: several accounts may share one proxy row, and
                # that raised MultipleResultsFound — killing the loop for the whole fleet.
                stmt = select(Account).where(Account.proxy_id == proxy.id)
                accounts = (await db.execute(stmt)).scalars().all()

                for account in accounts:
                    if account.status != "active":
                        continue
                    account.status = "sleeping"
                    # Without this the transition leaves no trace anywhere: the row
                    # changes, nothing says why, and the next person reads a fleet that
                    # went to sleep for no reason.
                    await telemetry.record(
                        db, event_type=telemetry.SLEEPING, account_id=account.id,
                        cause=f"proxy_unreachable proxy_id={proxy.id} "
                              f"strikes={n} url_host={proxy.url.rsplit('@', 1)[-1]}",
                    )
                    await db.commit()
                    slept += 1

                    log.warning("account_slept_proxy_unreachable",
                                account_id=account.id, proxy_id=proxy.id, strikes=n)

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

            # Structured, not `logging.getLogger(__name__)`: the app configures structlog,
            # so the stdlib logger this used to call was swallowed at root level. The loop
            # ran for ten days without emitting one line, which is why nobody saw the fleet
            # go down.
            log.info("health_check_cycle", proxies=len(proxies),
                     failing=len(strikes), slept=slept)
