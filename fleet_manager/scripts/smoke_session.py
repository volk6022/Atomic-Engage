#!/usr/bin/env python3
"""Safe single-account smoke test (milestone M0, step 1) — RUN ON A HOST WHERE THE
PROXY IS REACHABLE (i.e. your machine / the proxy provider has whitelisted that IP).

It builds the kurigram client EXACTLY as the fleet's StatelessManager does, connects
through the residential proxy, calls get_me(), and prints the result. It performs NO
outbound action (no messages, no joins) — zero ban risk beyond simply being online.

It also preserves the session's ORIGINAL device fingerprint (read from the seller's
metadata JSON). Never assign a different device_model/system_version to an existing
session — that is a confirmed ban trigger (Constitution Principle III).

Examples
--------
# preferred: a kurigram/pyrogram session string
# (proxy login carries the exit country, e.g. <login>__cr.us for a US exit)
python scripts/smoke_session.py \
    --meta /path/to/account.json \
    --session-string "BQ...." \
    --proxy "socks5://<login>__cr.us:<password>@np.puls-proxy.com:11000"

# config-only (no auth): just validate proxy reachability + geo coherence
python scripts/smoke_session.py --meta account.json \
    --proxy "socks5://<login>__cr.us:<password>@np.puls-proxy.com:11000" --check-only
"""
import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse


def parse_proxy(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ("socks5", "socks4", "http", "https"):
        raise SystemExit(f"unsupported proxy scheme: {u.scheme!r}")
    return {
        "scheme": "socks5" if u.scheme.startswith("socks") else "http",
        "hostname": u.hostname,
        "port": u.port,
        "username": u.username,
        "password": u.password,
    }


def fingerprint_from_meta(meta: dict) -> dict:
    """Map the seller metadata JSON onto kurigram Client fingerprint kwargs.

    Preserves the original device identity (Desktop/Android/iOS as-registered)."""
    syslang = meta.get("system_lang_pack") or "en-US"
    lang = (syslang.split("-")[0] if syslang else "en") or "en"
    return {
        "api_id": int(meta["app_id"]),
        "api_hash": meta["app_hash"],
        "device_model": meta.get("device") or "Desktop",
        "system_version": meta.get("sdk") or "Windows 10 x64",
        "app_version": meta.get("app_version") or "1.0",
        "lang_code": lang,
        "system_lang_code": syslang,
    }


def check_proxy_reachable(proxy: dict, timeout: float = 8.0) -> bool:
    import socket

    try:
        s = socket.create_connection((proxy["hostname"], proxy["port"]), timeout=timeout)
        s.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  proxy UNREACHABLE: {type(e).__name__}: {e}")
        return False


async def run(args) -> int:
    meta = json.load(open(args.meta, encoding="utf-8")) if args.meta else {}
    fp = fingerprint_from_meta(meta) if meta else {}
    phone = meta.get("phone")
    proxy = parse_proxy(args.proxy) if args.proxy else None

    print("=== fingerprint (preserved from session metadata) ===")
    for k, v in fp.items():
        print(f"  {k}: {v}")
    print(f"  phone: +{phone}  premium: {meta.get('is_premium')}")
    if proxy:
        print(f"=== proxy {proxy['scheme']}://{proxy['hostname']}:{proxy['port']} ===")
        reachable = check_proxy_reachable(proxy)
        print(f"  reachable: {reachable}")
        if not reachable:
            print("  -> run this on a host whose IP the proxy provider has whitelisted.")
            if not args.check_only:
                return 2

    # geo coherence (phone country vs proxy exit country) — informational
    try:
        import phonenumbers

        region = phonenumbers.region_code_for_number(phonenumbers.parse(f"+{phone}", None))
        print(f"=== geo: phone_country={region} (proxy code_location must match) ===")
    except Exception:
        pass

    if args.check_only:
        print("\ncheck-only: not connecting. Provide --session-string to do get_me().")
        return 0

    if not args.session_string and not args.session_file:
        print("\nNo --session-string/--session-file given; cannot authenticate.")
        print("This metadata JSON has no session_string. Provide one of:")
        print("  * a kurigram/pyrogram session string (preferred), or")
        print("  * a .session file, or convert tdata via opentele -> pyrogram session.")
        return 3

    from pyrogram import Client

    client_kwargs = dict(fp)
    client_kwargs["proxy"] = proxy
    client_kwargs["in_memory"] = True
    if args.session_string:
        client_kwargs["session_string"] = args.session_string
        name = "smoke"
    else:
        # a .session file path (without .session suffix) as the client name
        name = args.session_file.replace(".session", "")

    print("\n=== connecting (get_me only — no outbound action) ===")
    async with Client(name=name, **client_kwargs) as app:
        me = await app.get_me()
        print("  OK connected as:")
        print(f"    id={me.id} username=@{me.username} phone=+{me.phone_number}")
        print(f"    name={me.first_name} {me.last_name or ''} premium={me.is_premium}")
    print("  disconnected cleanly.")
    return 0


def main():
    p = argparse.ArgumentParser(description="Safe single-account smoke test (M0).")
    p.add_argument("--meta", help="seller account metadata JSON (for fingerprint)")
    p.add_argument("--session-string", help="kurigram/pyrogram session string")
    p.add_argument("--session-file", help="path to a .session file")
    p.add_argument("--proxy", help="socks5://user:pass@host:port")
    p.add_argument("--check-only", action="store_true", help="validate config/proxy, do not connect")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
