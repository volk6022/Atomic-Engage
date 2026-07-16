---
created: 2026-07-16
status: draft
confidence_level: high
---

# Atomic Engage — Usage Guide

Atomic Engage is a **fleet orchestration gateway** for automating Telegram user accounts (not bots). This guide covers how to use the Gateway API to onboard accounts, add proxies, execute actions, and monitor fleet status.

## Table of Contents

1. [Authentication](#authentication)
2. [Core Concepts](#core-concepts)
3. [API Endpoints](#api-endpoints)
   - [Fleet Status](#fleet-status)
   - [Account Lifecycle](#account-lifecycle)
   - [Proxy Management](#proxy-management)
   - [Actions (Messaging & Interactions)](#actions-messaging--interactions)
   - [API Credentials (Telegram app registration)](#api-credentials-telegram-app-registration)
   - [Admin Configuration](#admin-configuration)
4. [Webhook Events](#webhook-events)
5. [Working Examples](#working-examples)

---

## Authentication

All API requests must include the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-api-key-here" \
  http://localhost:8000/v1/fleet/status
```

Supported auth formats:
- Header: `X-API-Key: <key>`
- Bearer token: `Authorization: Bearer <key>`

---

## Core Concepts

### Accounts
A **Telegram user account** in the fleet. Each account has:
- Phone number and country of origin (geo-locked)
- Session string (imported or generated)
- Assigned proxy (residential, mobile 4G, or datacenter)
- Status: `WARMUP`, `ACTIVE`, `SLEEPING`, `BANNED`
- Warmup tier (progression through allowed actions based on use case)
- Daily action budget (per-account + per-subnet rate limits)

### Proxies
**Reserve proxy pool** managed separately. A proxy is:
- A URL (http, socks5, socks5h)
- Country (resolved via GeoIP or explicit hint)
- Type: residential, mobile_4g, datacenter
- State: reserve (in pool) or assigned (to an account)
- Health: periodically checked; unhealthy proxies are marked and auto-rotated

### Actions
Telegram operations queued from accounts:
- **Write actions**: `send_message`, `join_group`, `react`, `invite_to_group` (warmup-gated)
- **Read actions**: `resolve_username`, `get_chat_info`, `get_chat_history` (read-budget only, no warmup gate)

### Warmup Tiers
Accounts start at `FRESH` and progress through tiers (based on use case + time in fleet) before unlocking actions like cold DMs or group invitations. Tiers are defined in `config/safety.yaml` (hot-reloadable).

### Webhook Events
Async notifications pushed to your `webhook_url` when:
- Task completes or fails
- Account is banned / enters flood-wait
- Proxy fails or is swapped
- Warmup progresses or completes
- Incoming messages arrive

---

## API Endpoints

### Fleet Status

#### `GET /v1/fleet/status`
Returns account counts by status (warmup, active, sleeping, banned).

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/v1/fleet/status
```

Response:
```json
{
  "accounts": {
    "WARMUP": 10,
    "ACTIVE": 45,
    "SLEEPING": 2,
    "BANNED": 1
  }
}
```

#### `GET /v1/fleet/health`
Health check endpoint (no auth required).

```bash
curl http://localhost:8000/v1/fleet/health
```

Response:
```json
{
  "status": "ok"
}
```

#### `GET /v1/fleet/metrics`
Prometheus metrics (raw format).

```bash
curl http://localhost:8000/v1/fleet/metrics
```

---

### Account Lifecycle

#### `POST /v1/accounts/` — Onboard an Account
Import an existing Telegram session and register it in the fleet.

**Required fields:**
- `phone`: Phone number (with or without +)
- `session_string`: Exported Telegram session string
- `proxy_url`: Proxy URL for account use
- `use_case`: One of `reactions`, `join_groups`, `cold_dm`, `inviting`
- `proxy_country`: Optional ISO-3166 alpha-2 country code (inferred from GeoIP if omitted)

**Optional fields:**
- `proxy_type`: `residential` (default), `mobile_4g`, `datacenter`
- `tz_offset`: Timezone offset in seconds (defaults to 0)
- `work_start`, `work_end`: Working hours (8–22 by default)
- `cohort`: Experiment label
- `api_id`, `api_hash`: Preserve original Telegram app credentials (FR-146)
- `fingerprint`: Device fingerprint (device_model, system_version, app_version, lang_code, system_lang_code)

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+79991234567",
    "session_string": "...",
    "proxy_url": "http://user:pass@proxy.example.com:8080",
    "use_case": "cold_dm",
    "proxy_country": "US",
    "work_start": 9,
    "work_end": 21
  }' \
  http://localhost:8000/v1/accounts/
```

Response (201 Created):
```json
{
  "account_id": 42,
  "phone_country": "RU",
  "proxy_country": "US",
  "geo_status": "OK",
  "device_fingerprint": {
    "device_model": "SM-G950F",
    "system_version": "8.0.0",
    "app_version": "5.15.0"
  },
  "warmup_tier": "FRESH",
  "status": "WARMUP"
}
```

---

#### `GET /v1/accounts/{account_id}`
Retrieve account details.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/v1/accounts/42
```

Response:
```json
{
  "account_id": 42,
  "phone": "+79991234567",
  "phone_country": "RU",
  "status": "ACTIVE",
  "warmup_tier": "TIER_2",
  "use_case": "cold_dm",
  "warmup_day": 3,
  "proxy": {
    "id": 15,
    "country": "US",
    "type": "residential",
    "is_healthy": true
  }
}
```

---

#### `PUT /v1/accounts/{account_id}/proxy`
Reassign a new proxy to an account (e.g., due to IP-level bans or geoip rotation).

```bash
curl -X PUT \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "proxy_url": "http://user:pass@newproxy.example.com:8080",
    "proxy_country": "DE",
    "tz_offset": 3600
  }' \
  http://localhost:8000/v1/accounts/42/proxy
```

Response:
```json
{
  "account_id": 42,
  "proxy_country": "DE",
  "geo_status": "OK",
  "status": "ACTIVE"
}
```

---

#### `POST /v1/accounts/{account_id}/reactivate`
Reactivate a sleeping account (e.g., ban expired) with a new proxy.

**Preconditions:**
- Account must be in `SLEEPING` status
- New proxy must pass health check
- Geo-match must be valid

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "proxy_url": "http://user:pass@newproxy.example.com:8080",
    "proxy_country": "FR"
  }' \
  http://localhost:8000/v1/accounts/42/reactivate
```

Response:
```json
{
  "account_id": 42,
  "status": "ACTIVE",
  "proxy_country": "FR",
  "geo_status": "OK"
}
```

---

#### `POST /v1/accounts/{account_id}/unban`
Manually clear a banned account (if ban was issued in error or has expired externally).

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/v1/accounts/42/unban
```

Response:
```json
{
  "account_id": 42,
  "status": "ACTIVE",
  "previous_ban_reason": "flood_wait"
}
```

---

### Proxy Management

#### `POST /v1/proxies/` — Add Proxy to Reserve Pool
Register a proxy in the reserve pool (not yet assigned to any account).

**Fields:**
- `url`: Proxy URL
- `proxy_type`: `residential` (default), `mobile_4g`, `datacenter`
- `country`: Optional ISO-3166 country code (inferred from GeoIP if omitted)
- `tz_offset`: Optional timezone offset in seconds

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://user:pass@proxy.example.com:8080",
    "proxy_type": "residential",
    "country": "US"
  }' \
  http://localhost:8000/v1/proxies/
```

Response (201 Created):
```json
{
  "proxy_id": 123,
  "host": "proxy.example.com",
  "country": "US",
  "proxy_type": "residential",
  "state": "reserve",
  "is_healthy": true
}
```

---

#### `GET /v1/proxies/{proxy_id}`
Check proxy status and health.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/v1/proxies/123
```

Response:
```json
{
  "proxy_id": 123,
  "country": "US",
  "proxy_type": "residential",
  "state": "reserve",
  "is_healthy": true
}
```

---

### Actions (Messaging & Interactions)

#### `POST /v1/action` — Queue an Action
Submit a Telegram action for an account (send message, join group, react, etc.). Actions are queued asynchronously and executed by arq workers.

**Common fields:**
- `account_id`: Target account ID
- `action`: Action type (see below)
- `payload`: Action-specific payload dict
- `webhook_url`: URL to POST event notifications to
- `priority`: 1–10 (default 5); higher priority queues first

**Supported Actions:**

##### `send_message`
Send a text message to a user or channel.

Payload:
- `peer_id`: Telegram peer ID (user, group, or channel)
- `text`: Message text (max 4096 chars)
- `reply_to_message_id`: Optional ID of message to reply to

Example:
```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": 42,
    "action": "send_message",
    "payload": {
      "peer_id": 123456789,
      "text": "Hello from Atomic Engage!"
    },
    "webhook_url": "https://your-server.com/webhook"
  }' \
  http://localhost:8000/v1/action
```

Response (202 Accepted):
```json
{
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "queued",
  "account_id": 42
}
```

---

##### `join_group`
Join a group via invite link.

Payload:
- `invite_link`: Telegram invite link (t.me/joinchat/...)

---

##### `react`
React to a message with an emoji.

Payload:
- `peer_id`: Peer ID where message is
- `message_id`: Message ID to react to
- `reaction`: Emoji reaction (default: 👍)

---

##### `resolve_username`
Look up a Telegram username (read-only, no warmup gate).

Payload:
- `username`: Telegram username (without @)

---

##### `invite_to_group`
Invite a user to a group.

Payload:
- `group_username`: Group username
- `user_peer_id`: User ID to invite

---

##### `get_chat_info`
Fetch public profile/info for a user or channel (read-only).

Payload:
- `username`: Optional username
- `peer_id`: Optional peer ID (exactly one required)
- `with_pinned`: Include pinned messages (default true)

---

##### `get_chat_history`
Fetch recent public posts from a channel/user (read-only).

Payload:
- `username`: Optional username
- `peer_id`: Optional peer ID (exactly one required)
- `limit`: Number of posts (1–50, default 30)
- `min_date`: Optional ISO date string (pagination)
- `offset_id`: Optional message ID (pagination)

---

#### Action Execution Guarantees

**Security Gates (in order):**
1. Account must be in `ACTIVE` status (not BANNED or SLEEPING)
2. Geographic mismatch check (phone country vs proxy exit country)
3. **Warmup gate** (except for read-only actions): account must have progressed to a tier that unlocks the action
4. Per-account FIFO advisory lock (serialize actions)
5. Daily budget check (`budget.check_and_consume`) per account + api_id + /24-subnet
6. Humanization delay (configurable; default 60–300s between actions, scaled by `TIME_SCALE`)

If any gate fails, the action is not queued; instead, a `task_failed` or geo_reject webhook event is sent.

---

### API Credentials (Telegram app registration)

#### `POST /v1/api-credentials/` — Register a Telegram api_id
Register a Telegram app's api_id/api_hash for use by multiple accounts. Pool capacity: 100 credentials per instance.

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "api_id": 123456,
    "api_hash": "0123456789abcdef0123456789abcdef"
  }' \
  http://localhost:8000/v1/api-credentials/
```

Response (201 Created):
```json
{
  "credential_id": 5,
  "api_id": 123456,
  "account_count": 0
}
```

---

#### `GET /v1/api-credentials/{credential_id}`
Check credential and account count.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/v1/api-credentials/5
```

Response:
```json
{
  "id": 5,
  "api_id": 123456,
  "account_count": 3
}
```

---

### Admin Configuration

#### `GET /v1/admin/safety`
Fetch the current warmup schedules and rate limits (from `config/safety.yaml`, hot-reloadable).

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/v1/admin/safety
```

Response:
```json
{
  "warmup_tiers": {
    "FRESH": ["resolve_username"],
    "TIER_1": ["resolve_username", "get_chat_info"],
    "TIER_2": ["resolve_username", "get_chat_info", "react", "join_group"],
    "TIER_3": ["resolve_username", "get_chat_info", "react", "join_group", "send_message"]
  },
  "daily_budgets": {
    "send_message": 50,
    "join_group": 20,
    "react": 100
  }
}
```

---

#### `POST /v1/admin/reload-safety`
Reload `config/safety.yaml` without restarting (hot-reload). Useful for adjusting warmup schedules or rate limits on the fly.

```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/v1/admin/reload-safety
```

Response:
```json
{
  "reloaded": true,
  "warmup_tiers": { ... },
  "daily_budgets": { ... }
}
```

---

## Webhook Events

Your webhook endpoint will receive POST requests with JSON payloads for these events:

### Task Events

#### `task_complete`
Action succeeded.

```json
{
  "event": "task_complete",
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "account_id": 42,
  "result": {
    "telegram_message_id": 123,
    "peer_id": 987654321
  }
}
```

#### `task_failed`
Action failed with a Telegram error.

```json
{
  "event": "task_failed",
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "account_id": 42,
  "error_code": "USER_IS_BOT",
  "retry_count": 0
}
```

---

### Account Safety Events

#### `ban_alert`
Account was banned by Telegram.

```json
{
  "event": "ban_alert",
  "account_id": 42,
  "ban_reason": "flood_wait"
}
```

#### `geo_reject`
Action was rejected due to geographic mismatch (phone country ≠ proxy country).

```json
{
  "event": "geo_reject",
  "account_id": 42,
  "phone_country": "RU",
  "proxy_country": "US",
  "risk": "CRITICAL"
}
```

#### `flood_wait`
Telegram rate-limited the account; must wait before next action.

```json
{
  "event": "flood_wait",
  "account_id": 42,
  "flood_until": "2026-07-16T15:30:00Z",
  "retry_in": 3600
}
```

---

### Proxy Events

#### `proxy_fail_sleeping`
Proxy failed health check; account is put to sleep (can be reactivated with a new proxy).

```json
{
  "event": "proxy_fail_sleeping",
  "account_id": 42,
  "failed_proxy_id": 123,
  "reserve_available": false
}
```

#### `proxy_swap`
Proxy was rotated (healthy replacement found).

```json
{
  "event": "proxy_swap",
  "account_id": 42,
  "old_proxy_id": 123,
  "new_proxy_id": 124
}
```

---

### Warmup Events

#### `warmup_transition`
Account progressed to the next warmup tier.

```json
{
  "event": "warmup_transition",
  "account_id": 42,
  "from_tier": "FRESH",
  "to_tier": "TIER_1"
}
```

#### `warmup_complete`
Account reached max warmup tier (all actions unlocked).

```json
{
  "event": "warmup_complete",
  "account_id": 42
}
```

---

### Incoming Messages

#### `incoming_message`
Message received (if monitored).

```json
{
  "event": "incoming_message",
  "account_id": 42,
  "from_peer_id": 111222333,
  "message": "Hello back!",
  "message_id": 456,
  "date": "2026-07-16T14:30:00Z"
}
```

---

## Working Examples

### Full Workflow: Onboard, Add Proxy, Send Message

**1. Register Telegram app credentials:**
```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "api_id": 1234567,
    "api_hash": "abcdef1234567890abcdef1234567890"
  }' \
  http://localhost:8000/v1/api-credentials/
```

**2. Add proxies to the pool:**
```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://proxy_user:proxy_pass@us-east.proxy.example.com:8080",
    "proxy_type": "residential",
    "country": "US"
  }' \
  http://localhost:8000/v1/proxies/
```

**3. Onboard an account:**
```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+1234567890",
    "session_string": "1BVtsOMBu...",
    "proxy_url": "http://proxy_user:proxy_pass@us-east.proxy.example.com:8080",
    "use_case": "cold_dm",
    "proxy_country": "US",
    "work_start": 9,
    "work_end": 17
  }' \
  http://localhost:8000/v1/accounts/
```
Save the returned `account_id`.

**4. Queue a message action:**
```bash
curl -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": 42,
    "action": "send_message",
    "payload": {
      "peer_id": 987654321,
      "text": "Hi there! Interested in our service?"
    },
    "webhook_url": "https://your-server.com/webhook",
    "priority": 7
  }' \
  http://localhost:8000/v1/action
```

**5. Monitor your webhook for events:**
```json
{
  "event": "task_complete",
  "task_id": "uuid-here",
  "account_id": 42,
  "result": {
    "telegram_message_id": 789,
    "peer_id": 987654321
  }
}
```

---

## Deployment Notes

- **Port:** Default 8000 (configurable via `PORT` env var)
- **Database:** PostgreSQL + asyncpg (schema auto-created on startup)
- **Message Broker:** Redis + arq
- **Proxy Health Check:** Runs continuously in background; unhealthy proxies are auto-marked
- **Humanization:** Enabled by default; disable for tests via `HUMANIZE_ACTIONS=False`
- **Time Scaling:** Virtual time via `TIME_SCALE` (e.g., `TIME_SCALE=48` for 48× speedup in tests)

