# Telegram Fleet Orchestrator — API Service

REST gateway for a stateless Telegram task-queue that drives up to ~10 000 accounts.
Clients (typically **n8n**) submit *actions*; the gateway persists them as tasks,
an ARQ worker pool executes them through kurigram + per-account residential proxies,
and results are pushed back to a webhook.

- **Base URL**: `http://<host>:8010` (host-run) / service port in compose.
- **Auth**: every endpoint requires header **`X-API-Key: <API_KEY>`** (from `.env`). Missing/wrong key → `401`.
- **Content type**: `application/json`.
- **Interactive docs**: `GET /docs` (Swagger UI), `GET /openapi.json`.

---

## Core concepts

| Concept | Summary |
|---|---|
| **Account** | One Telegram identity: session string, device fingerprint (set once, never changed — FR-146), assigned proxy, `use_case`, `status`, `warmup_tier`. |
| **Proxy** | Residential/mobile/datacenter exit. `state` ∈ assigned/reserve. Country must match the account phone country (geo gate). |
| **Task** | A queued unit of work for one account. `status` ∈ `queued → executing → complete/failed`, plus `deferred`. |
| **use_case** | `reactions` / `join_groups` / `cold_dm` / `inviting` — selects the warmup schedule and rate limits. |
| **warmup_tier** | `fresh → basic → intermediate → ready`. Gates which actions an account may perform (see *Warmup gate*). |

### Task lifecycle & per-account FIFO

```
POST /v1/action ──► Task(queued) ──► ARQ ──► worker.run_task
                                              │
                       prepare() guards ──────┤ (banned/sleeping/geo/flood/hours)
                                              │
            per-account advisory-lock claim ──┤ strict FIFO: one EXECUTING task
                                              │ per account at a time (FR-021)
                                              ▼
                       kurigram call via proxy ──► complete / failed / deferred
                                              │
                                webhook POST  ▼   enqueue_next(account) → queue head
```

- **Strict FIFO (FR-021)**: a task is claimed for execution only under a Postgres
  per-account advisory lock, after checking that (a) no other task for that account
  is `executing` and (b) this task is the head of the account's queue. Two tasks for
  the **same** account can therefore never run concurrently; different accounts run
  in parallel. When a task finishes, `enqueue_next` dispatches the account's next
  queued task (priority desc, then FIFO by `created_at`, `id`).
- **At-least-once**: on worker crash, orphaned `executing` tasks older than the lease
  are reset to `queued` and re-enqueued at startup (US3).

### Warmup gate — "how warmed is an account?"

Warmedness is a function of **time** and **tier**:

1. An account starts `status=warmup`, `warmup_tier=fresh`, `warmup_day=0`.
2. Each `use_case` has a schedule (data-model §9, in `config/safety.yaml`) where every
   tier has a **duration in days**. The per-use-case totals are
   **reactions 7 · join_groups 14 · cold_dm 30 · inviting 45**.
3. `warmup_day` increases over time; when it reaches the **cumulative** day budget of
   the current tier, the account advances to the next tier. Reaching `ready` flips
   `status` to `active`.
4. Each tier permits a set of actions. The gateway enforces this: an action is accepted
   only if it is in the current tier's allowed list — e.g. a `reactions` account cannot
   `send_message` until `ready` (day 5+), and a `fresh` account cannot even `react` yet.
   The **read actions** (`resolve_username`, `get_chat_info`, `get_chat_history`) are
   exempt (read-only lookups, not behavioural risk).

Rejection looks like `409` with `detail: "Account not warmed for 'send_message' (use_case=reactions, warmup_tier=fresh). Allowed now: [...]"`.

The schedule and the daily **rate limits** live in `config/safety.yaml` and are
**hot-reloadable** without a restart (see *Admin*).

#### Warmup driver (what actually advances an account)

The warmup tiers are driven by a background cron, **`warmup_tick`**, that runs in the ARQ
worker (daily at 03:00 UTC, and once on worker startup — idempotent). On each tick, for
every `warmup` account it:

1. advances `warmup_day` to the real elapsed days since warmup started (`created_at`);
2. promotes the tier when the cumulative day budget is met, firing a
   `warmup_transition` webhook (or `warmup_complete` + flipping `status → active` at
   `ready`);
3. enqueues **one `warmup_action` per account per day** (the first action of the current
   tier's schedule; deduped in Redis by `warmup:done:{account_id}:{warmup_day}`). The
   `warmup_action` worker performs a lightweight, non-outbound client touch.

No request is needed to start warming — onboarding an account (created in `status=warmup`)
is enough; the next tick picks it up.

---

## Endpoints

### Actions

#### `POST /v1/action` → `202`
Submit an action for an account. Creates a `queued` task and enqueues it.

Request:
```json
{
  "account_id": 1,
  "action": "send_message",
  "payload": { "peer_id": 93372553, "text": "hello" },
  "webhook_url": "https://n8n.example.com/webhook/result",
  "priority": 5
}
```
`action` ∈ `send_message | join_group | react | resolve_username | invite_to_group | get_chat_info | get_chat_history`.
`priority` 1–10 (higher first). Per-action `payload`:

| action | payload fields |
|---|---|
| `send_message` | `peer_id:int`, `text:str(1..4096)`, `reply_to_message_id?:int` |
| `join_group` | `invite_link:str` |
| `react` | `peer_id:int`, `message_id:int`, `reaction:str="👍"` |
| `resolve_username` | `username:str` |
| `invite_to_group` | `group_username:str`, `user_peer_id:int` |
| `get_chat_info` | `username?:str` \| `peer_id?:int` (exactly one), `with_pinned:bool=true` |
| `get_chat_history` | `username?:str` \| `peer_id?:int`, `limit:int(1..50)=30`, `min_date?:ISO`, `min_id?:int`, `offset_id?:int` |

Response: `{ "task_id": "<uuid>", "status": "queued", "account_id": 1 }`.

#### Read actions (research-agent enrichment)

`resolve_username`, `get_chat_info` and `get_chat_history` are **read-only** lookups on
**public** Telegram entities (no behavioural footprint). They are **exempt from the
warmup gate** but consume a per-account daily **read budget** (`config/safety.yaml →
read_limits`: `get_chat_info` 200 · `get_chat_history` 80 · `resolve_username` 100); an
over-budget read is **deferred**, not run. `get_chat_info` results are cached in Redis
(`chatinfo:<username>`, ~7 days). `resolve_username` returns the peer plus enrichment
(`type`, `title`, `is_verified`, `is_scam`); on a channel/group it falls back to
`get_chat`. The result objects (`get_chat_info`: profile + `members_count` + pinned +
extracted urls/emails/phones; `get_chat_history`: `posts[]` with per-post contact
extraction) arrive in the `task_complete` webhook. See
`docs/research-agent-actions.md` §3–§4.

Errors: `422` invalid action · `404` account not found · `409` account banned/sleeping ·
`409` geo mismatch (account set to `sleeping` + `geo_reject` webhook) · `409` warmup gate.

### Accounts

- **`POST /v1/accounts/` → `201`** — onboard an imported session.
  Body: `phone`, `session_string`, `proxy_url`, `use_case`, optional
  `proxy_country` / `proxy_type` / `tz_offset` / `work_start` / `work_end`, and
  imported-identity fields `api_id` / `api_hash` / `fingerprint` (preserved verbatim,
  FR-146). Gates: phone-country derivable, proxy-country resolvable
  (explicit > GeoIP > login hint), **geo match** (phone vs proxy; datacenter rejected),
  api_id capacity. `422` / `409 geo_mismatch` on failure.
- **`GET /v1/accounts/{id}`** — account detail (`404` if absent).
- **`PUT /v1/accounts/{id}/proxy`** — reassign proxy (re-runs the geo gate).
- **`POST /v1/accounts/{id}/reactivate`** — `sleeping → active` (requires a healthy proxy).
- **`POST /v1/accounts/{id}/unban`** — clear ban state.

### Proxies

- **`POST /v1/proxies/` → `201`** — register a **reserve** proxy.
  Body: `url`, `proxy_type` (`mobile_4g|residential|datacenter`), `country?` (ISO-3166;
  required if not derivable), `tz_offset?`. Returns `proxy_id`, resolved `country`, `state="reserve"`.
- **`GET /v1/proxies/{id}`** — proxy detail.

### API credentials

- **`POST /v1/api-credentials/` → `201`** — register `{api_id, api_hash}` (duplicate `api_id` → `409`).
- **`GET /v1/api-credentials/{id}`** — credential detail (incl. `account_count`).

### Fleet / observability

- **`GET /v1/fleet/status`** — counts by account status / task status.
- **`GET /v1/fleet/health`** — `{ "status": "ok" }` liveness (DB/Redis state).
- **`GET /v1/fleet/metrics`** — Prometheus exposition (fleet counters).

### Admin — hot-reloadable safety config (FR-145)

- **`GET /v1/admin/safety`** — active config summary: `source` (file or `defaults`),
  `use_cases`, `warmup_totals`, `rate_limit_use_cases`.
- **`POST /v1/admin/reload-safety`** — re-read `config/safety.yaml` and apply it with
  no restart. Returns `{ "reloaded": true, ... }`.
  On POSIX, `kill -HUP <gateway_pid>` triggers the same reload.

`config/safety.yaml` holds `warmup_schedules` (per-tier `days` + allowed `actions`) and
`rate_limits` (per-use-case daily caps). Absent/invalid file → code defaults
(`app/core/safety_defaults.py`).

---

## Webhooks (callbacks to the client)

The worker POSTs JSON to the task's `webhook_url` (system events go to
`N8N_SYSTEM_WEBHOOK_URL`). Event shapes:

| event | when | key fields |
|---|---|---|
| `task_complete` | action succeeded | `task_id`, `result` (e.g. `telegram_message_id`) |
| `task_failed` | non-retryable failure | `task_id`, `error_code` |
| `flood_wait` | Telegram FloodWait | `task_id`, `account_id`, `flood_until` |
| `ban_alert` | ban detected | `task_id`, `account_id`, `reason` |
| `geo_reject` | runtime geo mismatch | `account_id`, `phone_country`, `proxy_country`, `risk` |
| `proxy_fail_sleeping` | proxy unhealthy, account parked | `account_id`, `failed_proxy_id` |
| `warmup_transition` / `warmup_complete` | tier advanced / reached `ready` | `account_id`, `from_tier`, `to_tier` |
| `incoming_message` | watcher saw an inbound DM (US2) | `account_id`, message fields |

---

## Environment (`.env`)

| var | purpose |
|---|---|
| `API_KEY` | gateway auth key (`X-API-Key`) |
| `DATABASE_URL` | `postgresql+asyncpg://…` |
| `REDIS_URL` | `redis://…` (ARQ queue + caches) |
| `N8N_SYSTEM_WEBHOOK_URL` | system-event sink (required) |
| `GEOIP_CITY_DB_PATH` / `GEOIP_ASN_DB_PATH` | optional MaxMind mmdb for proxy geo/ASN |
| `SAFETY_CONFIG_PATH` | optional override for `config/safety.yaml` |

## Running (host mode)

```bash
docker compose up -d postgres redis
PYTHONPATH=. uv run uvicorn app.main:app --host 0.0.0.0 --port 8010      # gateway
PYTHONPATH=. uv run python -m arq app.workers.arq_settings.WorkerSettings # worker
PYTHONPATH=. uv run python -m app.watchers.watcher_process                # watcher (US2)
```

> The Docker image build needs Docker Hub reachability; if blocked (e.g. behind a VPN),
> run on the host with `uv` as above.
