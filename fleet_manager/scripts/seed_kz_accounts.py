#!/usr/bin/env python3
"""One-shot seed: import the two KZ warmup accounts from C:/Users/.../tg-sessions.

For each `<id>_pyrogram.session` (pyrogram SQLite) it:
  * builds a kurigram session string straight from the SQLite (no network, no re-login;
    preserves the original auth key + api_id = identity preservation, FR-146),
  * reads the seller metadata `<id>.json` to preserve the ORIGINAL device fingerprint,
  * assigns the account a dedicated KZ sticky proxy (1:1), sessttl.60 rotation,
  * inserts api_credential / proxy / account rows with status=warmup, use_case=cold_dm.

Idempotent: skips an account whose session user_id already exists (matched via a probe
column is impossible since we don't store it, so we guard on phone+fingerprint dedupe
by refusing to run if the accounts table is already non-empty for these phones).

Run:  .venv/Scripts/python.exe scripts/seed_kz_accounts.py
"""
import asyncio
import base64
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

from sqlalchemy import select

from app.db.models import Account, ApiCredential, Proxy
from app.db.session import get_session_maker

SESSIONS_DIR = Path(r"C:/Users/ванечка малыш/Documents/tg-sessions")
PROXY_FILE = SESSIONS_DIR / "proxies-sticky.txt"

# kurigram session-string layout: dc_id, api_id, test_mode, auth_key(256), user_id, is_bot
SESSION_STRING_FORMAT = ">BI?256sQ?"

USE_CASE = "cold_dm"          # DM + (read-only) channel search/read; reads are warmup-exempt
COHORT = "kz_warmup_2026q2"
PHONE_COUNTRY = "KZ"
KZ_TZ_OFFSET = 18000          # Kazakhstan is UTC+5 (single zone since 2024)
WORK_START, WORK_END = 8, 22

# Deterministic 1:1 account -> KZ sticky-proxy port assignment.
ACCOUNT_PROXY_PORT = {
    "242583916": 11000,
    "243076629": 11001,
}


def build_session_string(session_path: Path, api_id: int) -> tuple[str, int]:
    con = sqlite3.connect(str(session_path))
    try:
        dc_id, test_mode, auth_key, user_id, is_bot = con.execute(
            "select dc_id, test_mode, auth_key, user_id, is_bot from sessions"
        ).fetchone()
    finally:
        con.close()
    if len(auth_key) != 256:
        raise ValueError(f"{session_path.name}: auth_key is {len(auth_key)} bytes, expected 256")
    packed = struct.pack(
        SESSION_STRING_FORMAT, dc_id, api_id, bool(test_mode), auth_key, user_id, bool(is_bot)
    )
    return base64.urlsafe_b64encode(packed).decode().rstrip("="), user_id


def kz_proxy_for_port(port: int) -> str:
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "__cr.kz" in line and line.rstrip().endswith(f":{port}"):
            return line
    raise SystemExit(f"no KZ sticky proxy on port {port} in {PROXY_FILE}")


def fingerprint_from_meta(meta: dict) -> dict:
    syslang = meta.get("system_lang_pack") or meta.get("system_lang_code") or "en"
    lang = meta.get("lang_code") or (syslang.split("-")[0] if syslang else "en") or "en"
    return {
        "device_model": (meta.get("device") or "Desktop")[:100],
        "system_version": (meta.get("sdk") or "Windows 10")[:20],
        "app_version": (meta.get("app_version") or "1.0")[:20],
        "lang_code": lang[:10],
        "system_lang_code": syslang[:10],
    }


async def main() -> int:
    session_maker = get_session_maker()
    async with session_maker() as db:
        existing = (await db.execute(select(Account))).scalars().all()
        if existing:
            print(f"accounts table already has {len(existing)} row(s); refusing to re-seed.")
            for a in existing:
                print(f"  id={a.id} phone={a.phone!r} status={a.status} tier={a.warmup_tier}")
            return 0

        # one shared api credential (Telegram Desktop api_id 2040)
        first_meta = json.loads((SESSIONS_DIR / "242583916.json").read_text(encoding="utf-8"))
        api_id = int(first_meta["app_id"])
        api_hash = first_meta["app_hash"]
        cred = (
            await db.execute(select(ApiCredential).where(ApiCredential.api_id == api_id))
        ).scalar_one_or_none()
        if cred is None:
            cred = ApiCredential(api_id=api_id, api_hash=api_hash, account_count=0)
            db.add(cred)
            await db.flush()

        for acc_id, port in ACCOUNT_PROXY_PORT.items():
            session_file = SESSIONS_DIR / f"{acc_id}_pyrogram.session"
            meta = json.loads((SESSIONS_DIR / f"{acc_id}.json").read_text(encoding="utf-8"))
            session_string, user_id = build_session_string(session_file, api_id)
            fp = fingerprint_from_meta(meta)

            phone = meta.get("phone") or ""
            if phone and not phone.startswith("+"):
                phone = f"+{phone}"

            proxy_url = kz_proxy_for_port(port)
            proxy = Proxy(
                url=proxy_url,
                proxy_type="residential",
                country=PHONE_COUNTRY,
                tz_offset=KZ_TZ_OFFSET,
                state="assigned",
                is_healthy=True,
            )
            db.add(proxy)
            await db.flush()

            account = Account(
                phone=phone,
                phone_country=PHONE_COUNTRY,
                session_string=session_string,
                api_credential_id=cred.id,
                proxy_id=proxy.id,
                device_model=fp["device_model"],
                system_version=fp["system_version"],
                app_version=fp["app_version"],
                lang_code=fp["lang_code"],
                system_lang_code=fp["system_lang_code"],
                use_case=USE_CASE,
                status="warmup",
                warmup_tier="fresh",
                warmup_day=0,
                work_start=WORK_START,
                work_end=WORK_END,
                cohort=COHORT,
            )
            db.add(account)
            cred.account_count += 1
            await db.flush()
            print(
                f"seeded acc={acc_id} user_id={user_id} phone={phone or '(unknown)'} "
                f"-> account_id={account.id} proxy=:{port} (KZ) "
                f"device={fp['device_model']!r} app={fp['app_version']!r}"
            )

        await db.commit()
        print(f"\nOK: 1 api_credential (api_id={api_id}) + 2 KZ proxies + 2 warmup accounts committed.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(asyncio.run(main()))
