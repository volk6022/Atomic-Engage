---
name: dispatch-local
description: Dispatch task batches to a local PI agent (Qwen3.5-9B on llama.cpp via pm2). Use when Claude Code needs to delegate parallelizable subtasks to a local agent to save API tokens. Handles tier selection (llama-server reconfiguration), pi invocation, and batch execution via agent_manager.py.
---

# Dispatch Local — Batch Manager for PI Agents

You are the orchestrator. This skill delegates parallelizable work to a local
`pi` agent (`@earendil-works/pi-coding-agent`) running on **Qwen3.5-9B** served by
**llama-server** (managed by pm2 on `:20022`).

**When to use**: parallelizable subtasks that don't need architectural decisions —
summarizing/analyzing files, research, code review by clear spec. The local agent
has read/write/bash tools, so it can read large source files *itself* — you pass it
a path and instructions, never the file contents. This is the main way to save tokens.

**When NOT to use**: architectural decisions, tasks needing your full context, or
chains where one task depends on another's output.

---

## Environment (verified 2026-06-07)

- **llama-server** runs under pm2 as process `llama-server` on port **20022**, model
  `Qwen3.5-9B.Q4_K_S.gguf` (a *reasoning* model — emits hidden reasoning before its
  answer, so give generous token budgets).
- **pi** is installed globally (`pi.cmd`), provider `local` → `:20022` configured in
  `~/.pi/agent/models.json` (model alias `qwen-local`). Verify: `pi --list-models local`.
- The batch manager lives at **`.claude/skills/dispatch-local/scripts/agent_manager.py`** (stdlib only).

### Two gotchas the script already handles (don't reinvent)

1. **pi `-p` hangs headless unless stdin is closed.** Print mode reads stdin until
   EOF; launched from a non-TTY its stdin pipe never closes and it hangs forever.
   The script passes `stdin=DEVNULL`. It also passes `--offline` (we only use the
   local provider; this skips flaky internet startup checks).
2. **Only one model fits in VRAM**, so tiers reconfigure the *same* pm2 server
   sequentially. The script snapshots the original pm2 args at start and **restores
   them in a finally block**, leaving the box as found.

---

## Tiers

Each tier reconfigures llama-server (`-c` = slots × ctx/slot, `-np` = slots) and runs
that many pi agents in parallel.

| Tier | slots (`-np`) | ctx/slot | total `-c` | Use for |
|------|---------------|----------|------------|---------|
| `small`  | 4 | 10k  |  40000 | short file-scope tasks (summaries of small docs) |
| `medium` | 3 | 40k  | 120000 | multi-file or larger single docs |
| `large`  | 2 | 100k | 200000 | deep analysis, very large files (still < 100k tokens) |

Pick by the **per-task** context: source file size + instructions must fit one slot.
A file over ~100k tokens (e.g. a >300KB notebook) does not fit any tier — split or
pre-trim it first. When unsure, go larger.

---

## Step 1: Build the tasks JSON

Write to `%TEMP%\local_batch_<run_id>.json` (UTF-8; the loader tolerates a BOM):

```json
{
  "run_id": "kb-sync-001",
  "working_dir": "C:/Users/bhunp/Documents/own_knowledge_base",
  "tasks": [
    {
      "id": "summary-ctx-sweep",
      "config": "small",
      "tools": true,
      "prompt": "Read the file at C:/path/report.md using your read tool. Write a faithful Markdown digest (title, 5-8 key-finding bullets, exact numbers/configs) to C:/.../raw/<domain>/<slug>.md using your write tool. Do not invent facts. Reply DONE when finished.",
      "timeout_seconds": 600
    }
  ]
}
```

Task fields: `id`, `prompt` (required); `config` (small|medium|large, default medium);
`tools` (default true — let pi read/write files); `thinking` (off|low|medium|high,
optional); `working_dir` (per-task override); `timeout_seconds` (default 600).

**Prompt pattern that works**: tell pi the exact source path, that it should use its
read tool, the exact output path, that it should use its write tool, "do not invent
facts", and to reply `DONE`. pi writes proper UTF-8.

---

## Step 2: Run the batch

```bash
# Dry run first — prints the tier plan and confirms the pm2 snapshot works:
python .claude/skills/dispatch-local/scripts/agent_manager.py %TEMP%\local_batch_kb-sync-001.json --dry-run

# Real run (reconfigures llama-server per tier, restores original on exit):
python .claude/skills/dispatch-local/scripts/agent_manager.py %TEMP%\local_batch_kb-sync-001.json

# Run against the currently-loaded config WITHOUT touching pm2:
python .claude/skills/dispatch-local/scripts/agent_manager.py %TEMP%\local_batch_kb-sync-001.json --no-pm2
```

Use `--no-pm2` when the loaded config already fits your tasks — it avoids the
~10-40s model reload per tier. Reconfiguration briefly takes the model offline, so
don't use the pm2-managing mode while something else needs `:20022`.

---

## Step 3: Read results

Results are written to `%TEMP%\local_results_<run_id>.json`:

```json
{
  "run_id": "kb-sync-001", "completed": 1, "failed": 0, "duration_seconds": 38.5,
  "tasks": [
    { "id": "summary-ctx-sweep", "status": "success", "exit_code": 0,
      "duration_seconds": 38.5, "stdout_tail": "DONE\n", "stderr_tail": "" }
  ]
}
```

When `tools: true`, the real output is the file pi wrote — open it to verify. For
failures: `stderr_tail` `"Connection error."` almost always means **llama-server is
down** (`pm2 list` → if stopped, `pm2 start llama-server`). Retry a task once with a
larger tier or more specific prompt; if it fails again, do it yourself.

---

## Effective prompt patterns

### File digest (the common case — saves the most tokens)
```
Read the file at <abs path> using your read tool. Then write a concise Markdown
digest to <abs out path> using your write tool: a title, 5-8 bullets of the key
findings, and any concrete numbers/configs/identifiers (keep them exact). Do not
invent facts; if something is unclear, say so. Reply with the single word DONE.
```

### Research (no file I/O)
```
{ "tools": false, "prompt": "Summarize the top 3 approaches to X with trade-offs..." }
```
With `tools:false` the result is pi's stdout (captured in `stdout_tail`); have it keep
output short, or use a tools task that writes to a file for long output.

---

## Notes / limits

- The model is slow (~20-25 tok/s/slot) and *reasons* before answering — a small-doc
  digest is ~5-40s. Batch generously; parallelism is your throughput lever.
- pi writes UTF-8; PowerShell's default `Get-Content` may show mojibake — read the
  file with a UTF-8 reader to confirm it's actually fine.
- Don't feed files larger than the tier's per-slot context. Guard sizes when building
  batches (a >100KB notebook will not fit; split or extract first).
```
