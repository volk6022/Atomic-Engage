#!/usr/bin/env python3
"""
Local PI Agent Batch Manager (pm2 + pi backend).

Claude Code dispatches tasks here. The manager reconfigures the SHARED pm2-managed
`llama-server` to the requested tier, runs `pi` agents in parallel against it
(one pi subprocess per slot), collects results, and restores the original server
configuration on exit.

Design notes (2026-06-07):
  - The machine runs ONE `llama-server` (Qwen3.5-9B) under pm2 on :20022. Only one
    model fits in VRAM, so tiers reconfigure the SAME server sequentially rather than
    spawning parallel servers. pm2 owns the process lifecycle (delete + start via an
    ecosystem file with interpreter:"none"); we never orphan a raw child process.
  - The ORIGINAL pm2 args are snapshotted at startup and restored in a finally block,
    so the box is left exactly as found.
  - `pi` (@earendil-works/pi-coding-agent) is driven in print mode. CRITICAL: pi's
    `-p` mode reads stdin until EOF; launched headless its stdin is an open pipe that
    never closes and it hangs forever. We pass stdin=DEVNULL so it gets immediate EOF.
  - pi reads/writes files itself via its tools (provider `local` → models.json →
    :20022), so the orchestrator never loads large source files into its own context.

Tiers (slots = pi concurrency = llama -np; ctx_per_slot drives -c = slots*ctx_per_slot):
  small  — 4 slots × 10k =  40k total
  medium — 3 slots × 40k = 120k total
  large  — 2 slots × 100k = 200k total

Usage:
  python agent_manager.py tasks.json
  python agent_manager.py tasks.json --dry-run
  python agent_manager.py tasks.json --no-pm2     # use running server as-is, no restart

Tasks JSON schema:
  {
    "run_id": "<optional>",
    "working_dir": "C:/path",          # default cwd for pi (tasks may override)
    "tasks": [
      {
        "id": "summary-foo",
        "prompt": "Read X with your read tool, write a digest to Y ...",
        "config": "small|medium|large",
        "tools": true,                 # default true (pi reads/writes files itself)
        "thinking": "off|low|medium|high",  # optional
        "working_dir": "C:/path",      # optional per-task override
        "timeout_seconds": 600
      }
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── Config ────────────────────────────────────────────────────────────────────

PM2_NAME = os.environ.get("LLAMA_PM2_NAME", "llama-server")
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "20022"))
LLAMA_MODELS_URL = f"http://localhost:{LLAMA_PORT}/v1/models"
PI_BIN = os.environ.get("PI_BIN") or shutil.which("pi") or "pi"
PI_PROVIDER = os.environ.get("PI_PROVIDER", "local")
PI_MODEL = os.environ.get("PI_MODEL", "qwen-local")
RESULTS_DIR = Path(os.environ.get("AGENT_RESULTS_DIR", tempfile.gettempdir()))

TIERS: dict[str, dict[str, int]] = {
    "small":  {"slots": 4, "ctx_per_slot": 10000},
    "medium": {"slots": 3, "ctx_per_slot": 40000},
    "large":  {"slots": 2, "ctx_per_slot": 100000},
}


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    prompt: str
    config: str = "medium"
    tools: bool = True
    thinking: str | None = None
    working_dir: str = "."
    timeout_seconds: int = 600


@dataclass
class TaskResult:
    id: str
    status: str  # success | failed | timeout
    duration_seconds: float
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class BatchResult:
    run_id: str
    completed: int
    failed: int
    duration_seconds: float
    tasks: list[TaskResult] = field(default_factory=list)


# ── pm2 / llama-server management ─────────────────────────────────────────────

def _pm2(*args: str) -> subprocess.CompletedProcess:
    # pm2's human output contains box-drawing/emoji bytes; force utf-8 to avoid
    # cp1252 decode errors in subprocess reader threads on Windows.
    return subprocess.run(["pm2", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", shell=True)


def snapshot_args() -> tuple[list[str] | None, str | None, str | None]:
    """Return (args, exec_path, cwd) for the running pm2 llama-server, or (None,...)."""
    try:
        data = json.loads(_pm2("jlist").stdout)  # json.loads tolerates pm2's dup keys
    except Exception:
        return None, None, None
    for p in data:
        if p.get("name") == PM2_NAME:
            env = p.get("pm2_env", {})
            return env.get("args"), env.get("pm_exec_path"), env.get("pm_cwd")
    return None, None, None


def tier_args(base_args: list[str], config: str) -> list[str]:
    """Take original args, swap -c and -np for the tier."""
    spec = TIERS[config]
    total_ctx = spec["slots"] * spec["ctx_per_slot"]
    out: list[str] = []
    skip = 0
    for i, a in enumerate(base_args):
        if skip:
            skip -= 1
            continue
        if a == "-c":
            out += ["-c", str(total_ctx)]
            skip = 1
        elif a == "-np":
            out += ["-np", str(spec["slots"])]
            skip = 1
        else:
            out.append(a)
    if "-c" not in out:
        out += ["-c", str(total_ctx)]
    if "-np" not in out:
        out += ["-np", str(spec["slots"])]
    return out


def wait_ready(timeout: int = 180) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(LLAMA_MODELS_URL, timeout=3) as r:
                meta = json.loads(r.read())["data"][0]["meta"]
                return meta.get("n_ctx")
        except Exception:
            time.sleep(2)
    return None


def reconfigure(exec_path: str, cwd: str, args: list[str], eco_path: str) -> int | None:
    eco = {"apps": [{
        "name": PM2_NAME, "script": exec_path, "interpreter": "none",
        "args": args, "cwd": cwd, "autorestart": True,
    }]}
    Path(eco_path).write_text(json.dumps(eco), encoding="utf-8")
    _pm2("delete", PM2_NAME)
    time.sleep(1)
    res = _pm2("start", eco_path)
    if res.returncode != 0:
        print(f"  [pm2] start rc={res.returncode}: {(res.stderr or res.stdout)[-300:]}", flush=True)
    return wait_ready()


# ── pi runner ─────────────────────────────────────────────────────────────────

def run_pi(task: Task) -> TaskResult:
    start = time.time()
    cmd = [PI_BIN, "-p", "--no-session", "--offline",
           "--provider", PI_PROVIDER, "--model", PI_MODEL]
    if not task.tools:
        cmd.append("--no-tools")
    if task.thinking:
        cmd += ["--thinking", task.thinking]
    cmd.append(task.prompt)
    env = os.environ.copy()
    env["PI_OFFLINE"] = "1"  # local provider only; skip flaky internet startup checks
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
            cwd=task.working_dir if os.path.isdir(task.working_dir) else None,
            timeout=task.timeout_seconds,
        )
        status = "success" if proc.returncode == 0 else "failed"
        dur = round(time.time() - start, 2)
        print(f"[task] {task.id}: {status} in {dur}s (exit={proc.returncode})", flush=True)
        return TaskResult(task.id, status, dur, proc.returncode,
                          (proc.stdout or "")[-600:], (proc.stderr or "")[-400:])
    except subprocess.TimeoutExpired:
        dur = round(time.time() - start, 2)
        print(f"[task] {task.id}: timeout in {dur}s", flush=True)
        return TaskResult(task.id, "timeout", dur, -1, "", "timed out")


# ── Batch runner ──────────────────────────────────────────────────────────────

def run_batch(tasks: list[Task], slots: int) -> list[TaskResult]:
    results: list[TaskResult] = []
    with ThreadPoolExecutor(max_workers=slots) as pool:
        futs = [pool.submit(run_pi, t) for t in tasks]
        for f in as_completed(futs):
            results.append(f.result())
    return results


def run_all(tasks: list[Task], run_id: str, manage_pm2: bool) -> BatchResult:
    start = time.time()
    grouped: dict[str, list[Task]] = {c: [] for c in TIERS}
    for t in tasks:
        grouped.setdefault(t.config, []).append(t)

    orig_args = exec_path = cwd = None
    eco_path = str(RESULTS_DIR / "llama_eco.json")
    if manage_pm2:
        orig_args, exec_path, cwd = snapshot_args()
        if not orig_args:
            print("[pm2] WARNING: could not snapshot llama-server; running without restart", flush=True)
            manage_pm2 = False

    all_results: list[TaskResult] = []
    try:
        for config in ("small", "medium", "large"):
            batch = grouped.get(config) or []
            if not batch:
                continue
            slots = TIERS[config]["slots"]
            if manage_pm2:
                print(f"[pm2] reconfigure -> {config} "
                      f"({slots}x{TIERS[config]['ctx_per_slot']})", flush=True)
                nctx = reconfigure(exec_path, cwd, tier_args(orig_args, config), eco_path)
                print(f"[pm2] ready, n_ctx/slot={nctx}", flush=True)
            print(f"[run] {config}: {len(batch)} task(s), parallel={slots}", flush=True)
            all_results.extend(run_batch(batch, slots))
    finally:
        if manage_pm2 and orig_args:
            print("[pm2] restoring original llama-server config", flush=True)
            nctx = reconfigure(exec_path, cwd, orig_args, eco_path)
            print(f"[pm2] restored, n_ctx/slot={nctx}", flush=True)

    order = {t.id: i for i, t in enumerate(tasks)}
    all_results.sort(key=lambda r: order.get(r.id, 0))
    completed = sum(1 for r in all_results if r.status == "success")
    return BatchResult(run_id, completed, len(all_results) - completed,
                       round(time.time() - start, 2), all_results)


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_tasks(path: str) -> tuple[str, list[Task], str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    run_id = data.get("run_id", str(uuid.uuid4()))
    wd_default = data.get("working_dir", ".")
    tasks = [Task(
        id=t["id"], prompt=t["prompt"], config=t.get("config", "medium"),
        tools=t.get("tools", True), thinking=t.get("thinking"),
        working_dir=t.get("working_dir", wd_default),
        timeout_seconds=int(t.get("timeout_seconds", 600)),
    ) for t in data["tasks"]]
    return run_id, tasks, wd_default


def save_results(result: BatchResult) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"local_results_{result.run_id}.json"
    out.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Local PI Agent Batch Manager (pm2 + pi)")
    ap.add_argument("tasks_file")
    ap.add_argument("--dry-run", action="store_true", help="print plan, do not execute")
    ap.add_argument("--no-pm2", action="store_true", help="do not reconfigure llama-server")
    args = ap.parse_args()

    run_id, tasks, _ = load_tasks(args.tasks_file)
    print(f"[agent_manager] run={run_id}, tasks={len(tasks)}, pi={PI_BIN}", flush=True)

    if args.dry_run:
        grouped: dict[str, int] = {}
        for t in tasks:
            grouped[t.config] = grouped.get(t.config, 0) + 1
        for c in ("small", "medium", "large"):
            if grouped.get(c):
                s = TIERS[c]
                print(f"  {c}: {grouped[c]} task(s) -> llama -c {s['slots']*s['ctx_per_slot']} "
                      f"-np {s['slots']}, pi parallel={s['slots']}")
        if not args.no_pm2:
            a, _, _ = snapshot_args()
            print(f"  pm2 snapshot {'OK' if a else 'FAILED'}; would restore original on exit")
        print("[dry-run] not executing.")
        return

    result = run_all(tasks, run_id, manage_pm2=not args.no_pm2)
    out = save_results(result)
    print(f"\n[done] {result.completed}/{len(tasks)} ok, {result.failed} failed, "
          f"{result.duration_seconds}s total\n[results] {out}", flush=True)
    if result.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
