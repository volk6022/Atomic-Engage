#!/usr/bin/env python3
"""Tiny local stand-in for the n8n webhook while n8n is down.

Accepts any POST, appends the JSON body (one per line) to logs/webhook_sink.jsonl and
prints a compact line so you can watch Watcher forwards live. Bind it to the same
host:port as N8N_SYSTEM_WEBHOOK_URL in .env (default 0.0.0.0:5678, path ignored).

Run:
  .venv/Scripts/python.exe scripts/webhook_sink.py --port 5678
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "webhook_sink.jsonl"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = {"_raw": raw.decode("utf-8", "replace")}
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False) + "\n")
        ev = body.get("event", "?")
        chat = body.get("chat_username") or body.get("chat_id")
        cp = body.get("is_channel_post")
        msg = (body.get("message") or "")[:70].replace("\n", " ")
        print(f"[{datetime.now():%H:%M:%S}] {ev} chat={chat} channel_post={cp} :: {msg}",
              flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence default access logging
        pass


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5678)
    args = ap.parse_args()
    print(f"[sink] listening on {args.host}:{args.port} -> {LOG}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
