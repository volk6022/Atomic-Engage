#!/usr/bin/env python3
"""Seed converted Telegram accounts into ONE client instance's isolated DB (M4).

Reuses the account-import shape from ``migrate_sessions.py`` but targets a specific
client's ``DATABASE_URL`` instead of a global one, and assigns each account a proxy
from THAT client's already-seeded reserve pool (see ``provision_client.py``) rather
than from a phone->proxy CSV.

Input ``accounts.json`` is the output of ``convert_sessions.py``::

    [{"phone", "session_string", "first_name", "last_name", "username"}, ...]

Targeting the per-client DB (postgres is internal-only per the compose template) —
run this INSIDE the instance's docker network, e.g.::

    docker compose --env-file .env -f docker-compose.yml \\
        run --rm -v /path/accounts.json:/tmp/accounts.json \\
        gateway python /app/scripts/seed_accounts.py --accounts-json /tmp/accounts.json

``--database-url`` overrides ``$DATABASE_URL`` if you can reach postgres directly.
``--dry-run`` parses + plans WITHOUT importing app/DB code or touching the network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Invoked by absolute path (`python /app/scripts/seed_accounts.py`), which puts
# /app/scripts on sys.path rather than /app, so `import app.db.models` misses.
# The image's editable install does not cover this either: it runs before the
# source is COPYed in, so there is nothing for it to link. Add the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def load_accounts(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list, got {type(data).__name__}")
    return data


def _phone_country(phone: str, fallback: str) -> str:
    """Best-effort ISO-2 region from the phone's country calling code."""
    try:
        import phonenumbers

        parsed = phonenumbers.parse(phone if phone.startswith("+") else f"+{phone}", None)
        region = phonenumbers.region_code_for_number(parsed)
        if region:
            return region
    except Exception:  # noqa: BLE001 - fall back to the proxy country
        pass
    return fallback


async def seed(
    accounts: list[dict],
    database_url: str,
    use_case: str,
    limit: int | None,
) -> int:
    # Point the app settings at THIS client's DB before importing app modules
    # (app.core.config reads DATABASE_URL from the environment at import time).
    os.environ["DATABASE_URL"] = database_url

    from sqlalchemy import select

    from app.db.models import Account, ApiCredential, Proxy
    from app.db.session import get_session_maker
    from app.services.fingerprint import DeviceFingerprintGenerator

    session_maker = get_session_maker()
    fingerprint_gen = DeviceFingerprintGenerator()

    if limit:
        accounts = accounts[:limit]

    success = skipped = failed = 0
    async with session_maker() as db:
        # Pull this client's reserve proxy pool once; assign round-robin.
        reserve = list(
            (
                await db.execute(
                    select(Proxy).where(Proxy.state == "reserve").order_by(Proxy.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not reserve:
            raise SystemExit(
                "no reserve proxies in this instance DB — run provision_client.py "
                "proxy-seed first"
            )
        rr = 0

        for acc in accounts:
            phone = (acc.get("phone") or "").strip()
            session_string = acc.get("session_string")
            if not phone or not session_string:
                skipped += 1
                continue
            try:
                proxy = reserve[rr % len(reserve)]
                rr += 1

                cred = (
                    await db.execute(
                        select(ApiCredential)
                        .order_by(ApiCredential.account_count.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if not cred:
                    print(f"No API credentials available for {phone}")
                    failed += 1
                    continue

                fp = fingerprint_gen.generate()
                account = Account(
                    phone=phone,
                    phone_country=_phone_country(phone, proxy.country),
                    session_string=session_string,
                    api_credential_id=cred.id,
                    proxy_id=proxy.id,
                    use_case=use_case,
                    status="warmup",
                    warmup_tier="fresh",
                    device_model=fp.device_model,
                    system_version=fp.system_version,
                    app_version=fp.app_version,
                    lang_code=fp.lang_code,
                    system_lang_code=fp.system_lang_code,
                    work_start=9,
                    work_end=22,
                )
                db.add(account)
                proxy.state = "active"
                cred.account_count += 1
                await db.commit()
                success += 1
                print(f"Imported: {phone} -> proxy {proxy.id} ({proxy.country})")
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                print(f"Failed: {phone} - {e}")
                failed += 1

    print(f"\nSummary: success={success}, skipped={skipped}, failed={failed}")
    return 0 if failed == 0 else 1


def dry_run(accounts: list[dict], limit: int | None) -> int:
    if limit:
        accounts = accounts[:limit]
    valid = [a for a in accounts if (a.get("phone") and a.get("session_string"))]
    print(f"[dry-run] accounts in file: {len(accounts)}")
    print(f"[dry-run] importable (phone + session_string present): {len(valid)}")
    for a in valid[:3]:
        print(f"[dry-run]   sample: phone={a.get('phone')} "
              f"name={a.get('first_name','')!r} username={a.get('username','')!r}")
    print("[dry-run] no DB touched; assignment happens against the reserve proxy pool "
          "at real run.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts-json", required=True, help="accounts.json from convert_sessions.py")
    p.add_argument("--database-url", default=None,
                   help="per-client DATABASE_URL (default: $DATABASE_URL)")
    p.add_argument("--use-case", default="reactions")
    p.add_argument("--limit", type=int, default=None, help="cap number of accounts")
    p.add_argument("--dry-run", action="store_true",
                   help="parse + plan only; no app import, no DB, no network")
    args = p.parse_args()

    accounts = load_accounts(Path(args.accounts_json))

    if args.dry_run:
        return dry_run(accounts, args.limit)

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("--database-url or $DATABASE_URL required for a real run")
    return asyncio.run(seed(accounts, database_url, args.use_case, args.limit))


if __name__ == "__main__":
    sys.exit(main())
