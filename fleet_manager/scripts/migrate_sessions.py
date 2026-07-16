#!/usr/bin/env python3
import argparse
import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.db.models import Account, Proxy, ApiCredential
from app.db.session import get_session_maker
from app.services.geo_match import GeoMatchValidator
from app.services.fingerprint import DeviceFingerprintGenerator
from app.services.proxy_manager import ProxyManager


async def main():
    parser = argparse.ArgumentParser(description="Import Telegram sessions")
    parser.add_argument(
        "--sessions-dir", required=True, help="Directory containing .session files"
    )
    parser.add_argument(
        "--proxy-csv", required=True, help="CSV with phone,proxy_url columns"
    )
    parser.add_argument("--use-case", default="reactions", help="Use case for accounts")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    use_case = args.use_case

    with open(args.proxy_csv) as f:
        reader = csv.DictReader(f)
        proxy_map = {row["phone"]: row["proxy_url"] for row in reader}

    session_files = list(sessions_dir.glob("*.session"))

    session_maker = get_session_maker()
    fingerprint_gen = DeviceFingerprintGenerator()
    proxy_manager = ProxyManager()
    geo_validator = GeoMatchValidator()

    success = 0
    skipped = 0
    failed = 0

    async with session_maker() as db:
        for session_file in session_files:
            phone = session_file.stem

            if phone not in proxy_map:
                skipped += 1
                continue

            try:
                with open(session_file) as f:
                    session_string = f.read().strip()

                proxy_url = proxy_map[phone]

                country, asn, tz_offset = geo_validator.get_proxy_info(
                    proxy_url.split("@")[1].split(":")[0]
                )

                result = geo_validator.validate(
                    phone_country="XX", proxy_country=country
                )
                if result.risk == "CRITICAL":
                    print(f"Geo validation failed for {phone}")
                    failed += 1
                    continue

                fingerprint = fingerprint_gen.generate()

                stmt = (
                    select(ApiCredential)
                    .order_by(ApiCredential.account_count.asc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                cred = result.scalar_one_or_none()

                if not cred:
                    print(f"No API credentials available for {phone}")
                    failed += 1
                    continue

                proxy = Proxy(
                    url=proxy_url,
                    proxy_type="residential",
                    country=country,
                    asn=asn,
                    tz_offset=tz_offset,
                    state="assigned",
                    is_healthy=True,
                )
                db.add(proxy)
                await db.flush()

                account = Account(
                    phone=phone,
                    phone_country=country,
                    session_string=session_string,
                    api_credential_id=cred.id,
                    proxy_id=proxy.id,
                    use_case=use_case,
                    status="warmup",
                    warmup_tier="fresh",
                    device_model=fingerprint.device_model,
                    system_version=fingerprint.system_version,
                    app_version=fingerprint.app_version,
                    lang_code=fingerprint.lang_code,
                    system_lang_code=fingerprint.system_lang_code,
                    work_start=9,
                    work_end=22,
                )
                db.add(account)
                await db.commit()

                success += 1
                print(f"Imported: {phone}")

            except Exception as e:
                print(f"Failed: {phone} - {e}")
                failed += 1

    print(f"\nSummary: success={success}, skipped={skipped}, failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
