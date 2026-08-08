#!/usr/bin/env python3
"""Onboard ONE self-owned account that logs in by phone+code and runs WITHOUT a proxy
(uses the host machine's own residential IP — higher reputation than the proxy pool).

Unlike scripts/seed_kz_accounts.py (which imports a purchased .session with a preserved
auth key), this account has no prior session, so we authenticate fresh by phone number
and mint the session string here. proxy_id is left NULL: the worker path already treats
a proxy-less account as "use host IP" (geo gate skipped, ASN gate skipped, no rotation).

Interactive login code / optional 2FA password are handed over via files (the harness
can't type into a background process):
  <workdir>/onboard.status    <- what the script is waiting for
  <workdir>/onboard.code      <- write the 5-6 digit login code here
  <workdir>/onboard.password  <- write the 2FA password here (only if asked)

Run:
  .venv/Scripts/python.exe scripts/onboard_local_account.py \
      --phone +79119374513 --country RU --workdir DIR
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.db.models import Account, ApiCredential
from app.db.session import get_session_maker

POLL_SECS = 2.0
WAIT_TIMEOUT_SECS = 600

# Telegram Desktop official api_id (matches the shared api_credential already seeded).
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

# A stable, plausible Telegram Desktop fingerprint. Reused verbatim on every login so the
# device identity never drifts (drift is a ban signal).
DEVICE = {
    "device_model": "Desktop",
    "system_version": "Windows 10",
    "app_version": "4.16.8 x64",
    "lang_code": "ru",
    "system_lang_code": "ru",
}


def _status(workdir: Path, text: str) -> None:
    (workdir / "onboard.status").write_text(text, encoding="utf-8")
    print(f"[onboard] {text}", flush=True)


def _wait_for_file(path: Path, what: str) -> str:
    deadline = time.time() + WAIT_TIMEOUT_SECS
    while time.time() < deadline:
        if path.exists():
            val = path.read_text(encoding="utf-8").strip()
            if val:
                path.unlink(missing_ok=True)
                return val
        time.sleep(POLL_SECS)
    raise TimeoutError(f"timed out waiting for {what} ({path.name})")


async def _get_or_create_account(db, phone: str, country: str, use_case: str, tier: str,
                                 work_start: int, work_end: int, cohort: str) -> Account:
    existing = (
        await db.execute(select(Account).where(Account.phone == phone))
    ).scalar_one_or_none()
    if existing is not None:
        print(f"[onboard] reusing existing account id={existing.id} status={existing.status}")
        return existing

    cred = (
        await db.execute(select(ApiCredential).where(ApiCredential.api_id == DEFAULT_API_ID))
    ).scalar_one_or_none()
    if cred is None:
        cred = ApiCredential(api_id=DEFAULT_API_ID, api_hash=DEFAULT_API_HASH, account_count=0)
        db.add(cred)
        await db.flush()

    account = Account(
        phone=phone,
        phone_country=country,
        session_string="",          # filled after login
        api_credential_id=cred.id,
        proxy_id=None,              # <- run on host IP, no proxy
        device_model=DEVICE["device_model"],
        system_version=DEVICE["system_version"],
        app_version=DEVICE["app_version"],
        lang_code=DEVICE["lang_code"],
        system_lang_code=DEVICE["system_lang_code"],
        use_case=use_case,
        status="warmup",
        warmup_tier=tier,
        warmup_day=0,
        work_start=work_start,
        work_end=work_end,
        cohort=cohort,
    )
    db.add(account)
    cred.account_count += 1
    await db.flush()
    print(f"[onboard] created account id={account.id} phone={phone} proxy=NONE (host IP)")
    return account


async def onboard(args, workdir: Path) -> int:
    from pyrogram import Client
    from pyrogram.errors import SessionPasswordNeeded

    session_maker = get_session_maker()
    async with session_maker() as db:
        account = await _get_or_create_account(
            db, args.phone, args.country, args.use_case, args.tier,
            args.work_start, args.work_end, args.cohort,
        )
        cred = (
            await db.execute(
                select(ApiCredential).where(ApiCredential.id == account.api_credential_id)
            )
        ).scalar_one()

        client = Client(
            name="onboard_local",
            api_id=cred.api_id,
            api_hash=cred.api_hash,
            proxy=None,                 # host IP
            device_model=account.device_model,
            system_version=account.system_version,
            app_version=account.app_version,
            lang_code=account.lang_code or "en",
            system_lang_code=account.system_lang_code or "en",
            in_memory=True,
        )

        interactive = workdir is None

        def _get_code() -> str:
            if interactive:
                return input("Enter the Telegram login code: ").strip()
            _status(workdir, f"WAITING_FOR_CODE; write code to onboard.code")
            return _wait_for_file(workdir / "onboard.code", "login code")

        def _get_password() -> str:
            if interactive:
                import getpass
                return getpass.getpass("Enter the 2FA password: ")
            _status(workdir, "WAITING_FOR_PASSWORD (2FA); write password to onboard.password")
            return _wait_for_file(workdir / "onboard.password", "2FA password")

        await client.connect()
        try:
            print("[onboard] sending code request (host IP, no proxy)...", flush=True)
            sent = await client.send_code(args.phone)
            print(f"[onboard] code sent (type={sent.type})", flush=True)
            code = _get_code()

            try:
                await client.sign_in(args.phone, sent.phone_code_hash, code)
            except SessionPasswordNeeded:
                pw = _get_password()
                await client.check_password(pw)

            me = await client.get_me()
            session_string = await client.export_session_string()
            print(
                f"[onboard] LOGGED IN id={me.id} @{me.username or '-'} "
                f"name={me.first_name!r} phone={me.phone_number or '-'}",
                flush=True,
            )
        finally:
            await client.disconnect()

        account.session_string = session_string
        account.status = "warmup"
        account.ban_reason = None
        account.banned_at = None
        account.flood_until = None
        await db.commit()
        msg = f"DONE: account id={account.id} onboarded, status=warmup, no proxy"
        if workdir is not None:
            _status(workdir, msg)
        else:
            print(f"[onboard] {msg}", flush=True)
        return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--country", default="RU")
    ap.add_argument("--use-case", dest="use_case", default="cold_dm")
    ap.add_argument("--tier", default="fresh")
    ap.add_argument("--work-start", dest="work_start", type=int, default=6,
                    help="UTC hour work window opens (no proxy => schedule is UTC; MSK=UTC+3)")
    ap.add_argument("--work-end", dest="work_end", type=int, default=20)
    ap.add_argument("--cohort", default="self_owned_2026q3")
    ap.add_argument("--workdir", default=os.environ.get("ONBOARD_WORKDIR"),
                    help="if omitted, code/password are prompted interactively via stdin")
    args = ap.parse_args()
    workdir = None
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(onboard(args, workdir))


if __name__ == "__main__":
    sys.exit(main())
