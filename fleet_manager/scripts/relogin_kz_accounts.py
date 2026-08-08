#!/usr/bin/env python3
"""Re-login a banned KZ account whose session was SESSION_REVOKED.

The stored auth key is dead (Telegram invalidated it when "all sessions" were
terminated), so we must re-authenticate by phone number and mint a fresh
session string. We reuse the SAME proxy + device fingerprint + api_id/api_hash
already in the DB so the new login looks 1:1 with the original identity.

Interactive parts (login code, optional 2FA password) are NOT read from stdin
(the harness can't type into a background process). Instead the script polls
for small files in a work dir:

  <workdir>/acct_<id>.status   <- script writes what it is waiting for
  <workdir>/acct_<id>.code     <- you write the 5-6 digit code here
  <workdir>/acct_<id>.password <- you write the 2FA password here (if asked)

Run (per account):
  .venv/Scripts/python.exe scripts/relogin_kz_accounts.py <account_id> [--workdir DIR]

On success: exports a new session_string, updates the account row
(status='warmup', ban_reason=NULL, banned_at=NULL) and commits.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.db.models import Account, ApiCredential, Proxy
from app.db.session import get_session_maker

POLL_SECS = 2.0
WAIT_TIMEOUT_SECS = 600  # 10 min to fetch the code from the seller dashboard


def _status(workdir: Path, acc_id: int, text: str) -> None:
    (workdir / f"acct_{acc_id}.status").write_text(text, encoding="utf-8")
    print(f"[acct {acc_id}] {text}", flush=True)


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


async def relogin(acc_id: int, workdir: Path) -> int:
    from pyrogram import Client
    from pyrogram.errors import SessionPasswordNeeded

    session_maker = get_session_maker()
    async with session_maker() as db:
        account = (
            await db.execute(select(Account).where(Account.id == acc_id))
        ).scalar_one_or_none()
        if account is None:
            print(f"no account id={acc_id}")
            return 2
        credential = (
            await db.execute(
                select(ApiCredential).where(ApiCredential.id == account.api_credential_id)
            )
        ).scalar_one()
        proxy = (
            await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        ).scalar_one_or_none()

        phone = account.phone
        print(
            f"[acct {acc_id}] phone={phone} device={account.device_model!r} "
            f"app={account.app_version!r} api_id={credential.api_id} "
            f"proxy={'yes' if proxy else 'none'}",
            flush=True,
        )

        client = Client(
            name=f"relogin_{acc_id}",
            api_id=credential.api_id,
            api_hash=credential.api_hash,
            proxy=proxy.url if proxy else None,
            device_model=account.device_model,
            system_version=account.system_version,
            app_version=account.app_version,
            lang_code=account.lang_code or "en",
            system_lang_code=account.system_lang_code or "en",
            in_memory=True,
        )

        await client.connect()
        try:
            _status(workdir, acc_id, "sending code request...")
            sent = await client.send_code(phone)
            _status(
                workdir,
                acc_id,
                f"WAITING_FOR_CODE (type={sent.type}); write code to acct_{acc_id}.code",
            )
            code = _wait_for_file(workdir / f"acct_{acc_id}.code", "login code")

            try:
                await client.sign_in(phone, sent.phone_code_hash, code)
            except SessionPasswordNeeded:
                _status(
                    workdir,
                    acc_id,
                    f"WAITING_FOR_PASSWORD (2FA); write password to acct_{acc_id}.password",
                )
                pw = _wait_for_file(workdir / f"acct_{acc_id}.password", "2FA password")
                await client.check_password(pw)

            me = await client.get_me()
            session_string = await client.export_session_string()
            print(
                f"[acct {acc_id}] LOGGED IN as id={me.id} "
                f"@{me.username or '-'} name={me.first_name!r}",
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
        _status(workdir, acc_id, "DONE: session refreshed, status=warmup")
        return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("account_id", type=int)
    ap.add_argument(
        "--workdir",
        default=os.environ.get("RELOGIN_WORKDIR", "."),
        help="dir for status/code/password handshake files",
    )
    args = ap.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(relogin(args.account_id, workdir))


if __name__ == "__main__":
    sys.exit(main())
