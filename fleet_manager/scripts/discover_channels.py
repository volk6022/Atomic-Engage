#!/usr/bin/env python3
"""Read-only discovery: find Russian-speaking communities where Python/ML/CV/AI freelance
ORDERS/PROJECTS are posted. Runs through a fleet account (real session + proxy, same path
as the orchestrator). Uses global search (contacts.Search) + similar-channels expansion
(get_similar_channels). Purely read-only -> warmup-exempt, no behavioural footprint.

Usage:  PYTHONPATH=. uv run python scripts/discover_channels.py --account-id 2
"""
import argparse
import asyncio
import json
import random
import sys
import warnings

warnings.simplefilter("ignore")

from app.db.session import get_session_maker
from app.services.stateless_manager import StatelessManager

KEYWORDS = [
    "python заказы", "python фриланс", "питон работа",
    "machine learning заказы", "ml фриланс", "нейросети заказы",
    "computer vision", "data science работа", "ai agents",
    "фриланс разработка", "it заказы", "удаленка python",
]

TECH = ["python", "питон", " ml", "machine learning", "машинн", "нейросет", "нейронк",
        "computer vision", " cv ", "data science", "дата", "ai", "искусствен", "llm",
        "gpt", "ml-", "ai-", "агент", "deep learning", "ds "]
ORDER = ["заказ", "фриланс", "freelance", "вакан", "работа", "проект", "биржа",
         "подработ", "исполнит", "требуется", "удалён", "удален", "job", "ваканси",
         "ищу разраб", "найм", "аутсорс", "оплат"]


def _cyr(s: str) -> bool:
    return any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in (s or ""))


def _score(text: str):
    t = (text or "").lower()
    tech = sorted({k.strip() for k in TECH if k.strip() in t})
    order = sorted({k.strip() for k in ORDER if k.strip() in t})
    return tech, order


def _uname(ch) -> str | None:
    u = getattr(ch, "username", None)
    if u:
        return u
    uns = getattr(ch, "usernames", None)
    if uns:
        for x in uns:
            a = getattr(x, "username", None)
            if a:
                return a
    return None


async def discover(client):
    from pyrogram.raw.functions.contacts import Search

    seen: dict[int, dict] = {}

    async def add_raw(ch):
        if not (getattr(ch, "broadcast", False) or getattr(ch, "megagroup", False)):
            return
        cid = getattr(ch, "id", None)
        if cid is None or cid in seen:
            return
        seen[cid] = {
            "id": cid,
            "username": _uname(ch),
            "title": getattr(ch, "title", None),
            "kind": "group" if getattr(ch, "megagroup", False) else "channel",
        }

    # 1) global search
    for kw in KEYWORDS:
        try:
            res = await client.invoke(Search(q=kw, limit=20))
            for ch in getattr(res, "chats", []) or []:
                await add_raw(ch)
        except Exception as e:  # noqa: BLE001
            print(f"  search '{kw}' -> {type(e).__name__}: {e}", file=sys.stderr)
        await asyncio.sleep(random.uniform(1.5, 3.0))

    print(f"  global-search candidates: {len(seen)}", file=sys.stderr)

    # 2) similar-channels expansion off the most promising seeds (by title relevance)
    seeds = sorted(
        seen.values(),
        key=lambda c: len(_score(c["title"])[0]) + len(_score(c["title"])[1]),
        reverse=True,
    )
    seeds = [s for s in seeds if s["username"]][:6]
    for s in seeds:
        try:
            similar = await client.get_similar_channels(s["username"])
            for ch in similar or []:
                cid = ch.id
                if cid not in seen:
                    seen[cid] = {
                        "id": cid, "username": getattr(ch, "username", None),
                        "title": getattr(ch, "title", None),
                        "kind": "group" if getattr(ch, "type", None) and "GROUP" in str(ch.type) else "channel",
                    }
        except Exception as e:  # noqa: BLE001
            print(f"  similar '{s['username']}' -> {type(e).__name__}: {e}", file=sys.stderr)
        await asyncio.sleep(random.uniform(1.5, 3.0))

    print(f"  after similar-channels: {len(seen)}", file=sys.stderr)

    # 3) resolve details (members, description) for candidates that pass a cheap title gate
    prelim = []
    for c in seen.values():
        if not c["username"]:
            continue
        tech, order = _score(c["title"])
        if tech or order:  # title hints relevance -> worth a get_chat
            prelim.append(c)
    prelim = prelim[:35]

    enriched = []
    for c in prelim:
        try:
            chat = await client.get_chat(c["username"])
        except Exception as e:  # noqa: BLE001
            print(f"  get_chat '{c['username']}' -> {type(e).__name__}: {e}", file=sys.stderr)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            continue
        title = getattr(chat, "title", "") or ""
        desc = getattr(chat, "description", "") or ""
        members = getattr(chat, "members_count", None)
        tech, order = _score(title + " \n " + desc)
        russian = _cyr(title) or _cyr(desc)
        enriched.append({
            "username": c["username"],
            "title": title,
            "members": members,
            "kind": c["kind"],
            "russian": russian,
            "tech": tech,
            "order": order,
            "score": len(tech) + 2 * len(order) + (1 if russian else 0),
            "description": desc[:200],
        })
        await asyncio.sleep(random.uniform(1.2, 2.5))

    return enriched


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, default=2)
    args = ap.parse_args()

    sm = StatelessManager()
    async with get_session_maker()() as db:
        results = await sm.execute(args.account_id, discover, db)

    # final filter: Russian + at least one order keyword + at least one tech keyword
    good = [r for r in results if r["russian"] and r["order"] and r["tech"]]
    good.sort(key=lambda r: (r["score"], r["members"] or 0), reverse=True)

    print(json.dumps(good, ensure_ascii=False, indent=2))
    print(f"\nTOTAL matched: {len(good)} (from {len(results)} enriched)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
