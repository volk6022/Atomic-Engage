"""Phase 3 (US2) genuine onboarding & account-lifecycle tests — real DB, real routes.

Geo is resolved from an explicit proxy_country (no GeoIP mmdb needed in CI). Proxy
TCP health (reactivate) is the only network seam — monkeypatched.
"""
import random
import uuid

import pytest
from sqlalchemy import func, select

from app.core.constants import AccountStatus, WarmupTier
from app.db.models import Account, ApiCredential, Proxy

API_KEY = "change_me_in_production"
H = {"X-API-Key": API_KEY}

# Telegram Desktop fingerprint from the real validation session (FR-146).
DESKTOP_FP = {
    "device_model": "TUF516PE-AB73",
    "system_version": "Windows 10 x64",
    "app_version": "6.6.2 x64",
    "lang_code": "en",
    "system_lang_code": "en-US",
}


def _us_phone() -> str:
    # Valid NANP: area 346 (Houston), subscriber NXX-XXXX with first digit 2-9.
    return f"+1346{random.randint(2, 9)}{random.randint(0, 999999):06d}"


def _onboard_body(**over):
    body = {
        "phone": _us_phone(),
        "session_string": "test_session_string",
        "proxy_url": "socks5://u__cr.us:p@np.example.com:11000",
        "use_case": "reactions",
        "proxy_country": "US",
        "proxy_type": "residential",
        "api_id": 2040,
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "fingerprint": DESKTOP_FP,
    }
    body.update(over)
    return body


# --- C6: routers registered + auth enforced --------------------------------------
@pytest.mark.asyncio
async def test_routers_registered_and_authed(async_client):
    # unauth -> 401 (not 404): route exists and is protected
    r = await async_client.post("/v1/accounts/", json={})
    assert r.status_code == 401, r.text
    r = await async_client.post("/v1/proxies/", json={})
    assert r.status_code == 401
    r = await async_client.post("/v1/api-credentials/", json={})
    assert r.status_code == 401


# --- US3 sc1: geo-OK onboarding persists, preserving imported identity (FR-146) ----
@pytest.mark.asyncio
async def test_s3_sc1_onboarding_persists_and_preserves_fingerprint(async_client, session_maker):
    r = await async_client.post("/v1/accounts/", json=_onboard_body(), headers=H)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["geo_status"] == "OK"
    assert data["status"] == AccountStatus.WARMUP
    assert data["warmup_tier"] == WarmupTier.FRESH
    assert data["device_fingerprint"]["device_model"] == "TUF516PE-AB73"

    async with session_maker() as s:
        acc = (await s.execute(select(Account).where(Account.id == data["account_id"]))).scalar_one()
        cred = (await s.execute(select(ApiCredential).where(ApiCredential.id == acc.api_credential_id))).scalar_one()
    # imported identity preserved
    assert acc.device_model == "TUF516PE-AB73"
    assert acc.system_version == "Windows 10 x64"
    assert cred.api_id == 2040
    assert cred.account_count == 1
    assert acc.phone_country == "US"


# --- US3 sc2: geo mismatch -> 422 and NO rows created ------------------------------
@pytest.mark.asyncio
async def test_s3_sc2_geo_mismatch_creates_nothing(async_client, session_maker):
    async with session_maker() as s:
        before = (await s.execute(select(func.count()).select_from(Account))).scalar_one()

    body = _onboard_body(proxy_country="RU")  # US phone vs RU proxy -> CRITICAL
    r = await async_client.post("/v1/accounts/", json=body, headers=H)
    assert r.status_code == 422, r.text
    assert "geo_mismatch" in r.json()["detail"]

    async with session_maker() as s:
        after = (await s.execute(select(func.count()).select_from(Account))).scalar_one()
    assert after == before  # nothing persisted


# --- datacenter proxy rejected for account ops (Principle VI) ----------------------
@pytest.mark.asyncio
async def test_onboard_rejects_datacenter_proxy(async_client):
    r = await async_client.post("/v1/accounts/", json=_onboard_body(proxy_type="datacenter"), headers=H)
    assert r.status_code == 422
    assert "datacenter" in r.json()["detail"]


# --- US3 sc3: batch onboarding -> unique fingerprints when generated; no shared proxy
@pytest.mark.asyncio
async def test_s3_sc3_no_shared_proxy_across_accounts(async_client, session_maker):
    ids = []
    for _ in range(5):
        # omit fingerprint -> generator path; distinct proxy urls
        body = _onboard_body(fingerprint=None, api_id=None, api_hash=None,
                             proxy_url=f"socks5://u__cr.us:p@np.example.com:{11000 + uuid.uuid4().int % 900}")
        # ensure an api credential exists for the pool path
        await async_client.post("/v1/api-credentials/", json={"api_id": uuid.uuid4().int % 9_000_000 + 1000, "api_hash": "x" * 32}, headers=H)
        r = await async_client.post("/v1/accounts/", json=body, headers=H)
        assert r.status_code == 201, r.text
        ids.append(r.json()["account_id"])

    async with session_maker() as s:
        accs = (await s.execute(select(Account).where(Account.id.in_(ids)))).scalars().all()
    proxy_ids = [a.proxy_id for a in accs]
    assert len(set(proxy_ids)) == len(proxy_ids)  # 1:1, no shared proxy


# --- GET account detail ------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_account_detail(async_client):
    r = await async_client.post("/v1/accounts/", json=_onboard_body(), headers=H)
    aid = r.json()["account_id"]
    g = await async_client.get(f"/v1/accounts/{aid}", headers=H)
    assert g.status_code == 200, g.text
    data = g.json()
    assert data["account_id"] == aid
    assert data["phone_country"] == "US"
    assert data["proxy"]["country"] == "US"
    assert data["warmup_tier"] == WarmupTier.FRESH
    # unknown id -> 404
    assert (await async_client.get("/v1/accounts/999999999", headers=H)).status_code == 404


# --- reassign proxy: geo-OK swaps, geo-mismatch rejected ---------------------------
@pytest.mark.asyncio
async def test_reassign_proxy(async_client, session_maker):
    aid = (await async_client.post("/v1/accounts/", json=_onboard_body(), headers=H)).json()["account_id"]

    # geo-mismatch (US account -> RU proxy) rejected
    bad = await async_client.put(
        f"/v1/accounts/{aid}/proxy",
        json={"proxy_url": "socks5://u__cr.ru:p@np.example.com:11000", "proxy_country": "RU"},
        headers=H,
    )
    assert bad.status_code == 422

    # geo-OK swap to another US proxy
    ok = await async_client.put(
        f"/v1/accounts/{aid}/proxy",
        json={"proxy_url": "socks5://u__cr.us:p@np.example.com:11050", "proxy_country": "US"},
        headers=H,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["proxy_country"] == "US"
    async with session_maker() as s:
        acc = (await s.execute(select(Account).options().where(Account.id == aid))).scalar_one()
        new_proxy = (await s.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one()
    assert new_proxy.url.endswith(":11050")
    assert new_proxy.state == "assigned"


# --- proxies reserve pool ----------------------------------------------------------
@pytest.mark.asyncio
async def test_create_reserve_proxy(async_client, session_maker):
    r = await async_client.post(
        "/v1/proxies/",
        json={"url": "socks5://u__cr.de:p@np.example.com:11000", "proxy_type": "residential"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["state"] == "reserve" and data["country"] == "DE"
    async with session_maker() as s:
        proxy = (await s.execute(select(Proxy).where(Proxy.id == data["proxy_id"]))).scalar_one()
    assert proxy.state == "reserve"


# --- api credentials: duplicate -> 409 --------------------------------------------
@pytest.mark.asyncio
async def test_api_credential_duplicate_conflict(async_client):
    api_id = uuid.uuid4().int % 9_000_000 + 100
    r1 = await async_client.post("/v1/api-credentials/", json={"api_id": api_id, "api_hash": "x" * 32}, headers=H)
    assert r1.status_code == 201, r1.text
    r2 = await async_client.post("/v1/api-credentials/", json={"api_id": api_id, "api_hash": "x" * 32}, headers=H)
    assert r2.status_code == 409


# --- unban: banned -> active -------------------------------------------------------
@pytest.mark.asyncio
async def test_unban_restores_account(async_client, account_factory, session_maker):
    ids = await account_factory(status="banned")
    # set a ban reason
    async with session_maker() as s:
        async with s.begin():
            acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
            acc.ban_reason = "UserDeactivated"
    r = await async_client.post(f"/v1/accounts/{ids['account_id']}/unban", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["previous_ban_reason"] == "UserDeactivated"
    async with session_maker() as s:
        acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
    assert acc.status == AccountStatus.ACTIVE and acc.ban_reason is None


# --- reactivate: sleeping -> active when proxy healthy (health TCP monkeypatched) --
@pytest.mark.asyncio
async def test_reactivate_sleeping_account(async_client, account_factory, session_maker, monkeypatch):
    from app.services.proxy_manager import ProxyManager

    async def _healthy(self, url, timeout=10.0):
        return True

    monkeypatch.setattr(ProxyManager, "health_check", _healthy)

    ids = await account_factory(status="sleeping", phone_country="US", proxy_country="US")
    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": "socks5://u__cr.us:p@np.example.com:11000", "proxy_country": "US"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    async with session_maker() as s:
        acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
    assert acc.status == AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_reactivate_unhealthy_proxy_conflict(async_client, account_factory, monkeypatch):
    from app.services.proxy_manager import ProxyManager

    async def _unhealthy(self, url, timeout=10.0):
        return False

    monkeypatch.setattr(ProxyManager, "health_check", _unhealthy)

    ids = await account_factory(status="sleeping", phone_country="US", proxy_country="US")
    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": "socks5://u__cr.us:p@np.example.com:11000", "proxy_country": "US"},
        headers=H,
    )
    assert r.status_code == 409
    assert "proxy_unhealthy" in r.json()["detail"]
