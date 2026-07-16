# Read Actions for the Research-Agent Backfill

**Status**: proposal · **Created**: 2026-06-15 · **Audience**: fleet_manager maintainers
**Consumer**: `Atomic-Scraper-Service` research agent (org-card enrichment)

## 1. Why

Today the fleet exposes only **outbound, behaviour-risky** actions
(`send_message`, `join_group`, `react`, `invite_to_group`) plus one infra lookup
(`resolve_username`). The research agent needs the opposite: **read-only lookups**
that enrich an organisation card from its public Telegram presence. The 3-zone
run captured only **49 Telegram handles across 300 orgs** because research ran
with `NO_TELEGRAM=1` (t.me unreachable from the scraper host). The fleet — with
live sessions + geo-coherent RU proxies — is the right place to do this read work.

These actions are **not** outreach. They are the "Telegram backfill via the fleet"
named as the next step in `reports/003-e2e-session-and-fixes-2026-06-13.md` §10.

## 2. What the research agent actually needs

Mapping the org-card schema (`Atomic-Scraper-Service/yandex_enrichment_experiment/02_research_orgs.py`
→ `ORG_CARD_SCHEMA`) to the Telegram capability that fills it:

| Org-card field | Telegram source | Action |
|---|---|---|
| `social.telegram[]` — validate handle, get `peer_id` | username resolution | `resolve_username` *(exists — enrich)* |
| `what_they_do`, `scale_indicators` (subscribers) | channel/group **About** + `members_count` | **`get_chat_info`** *(new, P1)* |
| `contacts.websites/emails/phones` | links/emails/phones in bio · About · pinned msg | **`get_chat_info`** *(new, P1)* |
| `problems_signals`, `vacancies`, activity recency | last N **public** channel posts | **`get_chat_history`** *(new, P1)* |
| `social.telegram[]` when web research found **no** handle | name → candidate channels | **`search_public_chat`** *(new, P2, best-effort)* |

Everything here reads **public** entities. Anything requiring **joining** a closed
group, reading discussion/comment threads, or downloading media is **out of scope**
(it crosses back into behavioural/ban-risk territory — see §7).

## 3. Proposed actions

All new actions follow the existing worker pattern
(`app/workers/<action>.py` → `run_task(ctx, task_id, builder, post_process=)`,
builder returns `async def action(client)` that calls kurigram). Each must be:
registered in `app/workers/arq_settings.py`, added to the action enum in
`app/api/v1/actions.py` (and its `/docs` description), given a Pydantic payload
model, **exempted from the warmup gate** (read = infra, like `resolve_username`,
`actions.py:132`), and **rate-limited** (see §4).

### 3.1 `resolve_username` — ENHANCE (low effort)

Currently returns `{peer_id, username, access_hash}` only. Add the peer **type**
and (cheap, already on the object) verified/scam flags so the agent can route:

```jsonc
// result
{ "peer_id": 93372553, "username": "acme", "access_hash": 123,
  "type": "channel|supergroup|group|bot|user",
  "title": "ACME", "is_verified": false, "is_scam": false }
```
kurigram: keep `get_users`; on empty (channel/group) fall back to `get_chat` to
populate `type`/`title`. Feeds the decision of whether to call `get_chat_info`
/ `get_chat_history` next.

### 3.2 `get_chat_info` — NEW (P1)

Full public profile of a user/bot/channel/group.

```jsonc
// payload  (exactly one of username | peer_id required)
{ "username": "acme", "peer_id": null, "with_pinned": true }
```
kurigram: `chat = await client.get_chat(chat_id)`; if `with_pinned` and
`chat.pinned_message` is absent, `await client.get_chat_history(chat_id, limit=1)`
is **not** used here — read the `pinned_message` already attached to `Chat`.
Members count: `chat.members_count` (present for channels/groups); else
`await client.get_chat_members_count(chat_id)`.

```jsonc
// result
{ "peer_id": 93372553, "type": "channel", "title": "ACME",
  "username": "acme", "description": "...About text...",
  "members_count": 5821, "is_verified": false, "is_scam": false,
  "linked_chat_username": "acme_chat",
  "pinned_message_text": "...",
  "extracted": { "urls": ["acme.ru"], "emails": ["hi@acme.ru"], "phones": ["+7..."] } }
```
`extracted` = entity/regex sweep over `description` + `bio` + `pinned_message_text`
(URL/email/phone). This directly fills `what_they_do`, `scale_indicators`
(subscriber count is a strong scale signal), and `contacts.*`.

### 3.3 `get_chat_history` — NEW (P1)

Last N posts of a **public** channel (recency, content signals, contacts in posts).

```jsonc
// payload
{ "username": "acme", "peer_id": null, "limit": 30, "min_date": "2026-01-01" }
```
- `limit` **hard-capped ≤ 50** (validator); `min_date` optional early-stop.
- kurigram: `async for m in client.get_chat_history(chat_id, limit=limit): ...`
  Stop when `m.date < min_date`. Public channels are readable **without joining**;
  if the call raises `ChannelPrivate`/`UsernameNotOccupied`, return `null` (handled).

```jsonc
// result
{ "count": 30, "newest_date": "2026-06-14", "oldest_date": "2026-04-02",
  "posts": [ { "message_id": 412, "date": "2026-06-14", "text": "...",
               "views": 1503, "has_media": true,
               "urls": ["..."], "emails": [], "phones": [] } ] }
```
Feeds `problems_signals` (complaints/pain in posts), `vacancies` (hiring posts),
`contacts.*`, and an activity/recency signal (dead channel vs active).
**Heavier than the others → stricter rate budget** (§4).

### 3.4 `search_public_chat` — NEW (P2, best-effort discovery)

When web research found **no** handle: try to discover the org's channel by name.

```jsonc
// payload
{ "query": "ACME СПб", "limit": 5 }
```
kurigram: Telegram `contacts.search` via raw invoke
(`client.invoke(raw.functions.contacts.Search(q=query, limit=limit))`), returning
chat candidates. Result = ranked `[{username, title, type, members_count}]` for the
**LLM to disambiguate** (do not auto-accept — high false-positive rate). Phase 2;
ship P1 first and measure how often a handle is genuinely missing.

## 4. Cross-cutting design changes

These matter more than the per-action code — they keep reads from burning the fleet.

1. **A `read`/`infra` action class, warmup-exempt but rate-limited.** Generalise the
   `resolve_username` exemption (`actions.py:132`) to a set —
   `READ_ACTIONS = {resolve_username, get_chat_info, get_chat_history, search_public_chat}`.
   Reads skip the warmup gate (no behavioural footprint) but **still** consume a
   per-account daily budget via `rate_limit_increment` (pattern already in
   `app/services/peer_resolver.py:30`). Suggested caps in `config/safety.yaml`
   (hot-reloadable): `get_chat_info` 200/day, `get_chat_history` 80/day,
   `search_public_chat` 50/day, per account. `get_chat_history` weighted heaviest.

2. **A dedicated reader pool / `use_case="research_read"`** so heavy reads do **not**
   run on accounts being warmed for `cold_dm`. Add the use_case to the warmup
   schedule (its tiers can be trivially short since reads are gateless) and to
   `app/db/models.py`. Separation means a read-triggered limit/ban never touches an
   outreach-warmed identity.

3. **Account-agnostic dispatch for reads.** The scraper must not manage `account_id`s.
   Allow `account_id` to be **omitted** on read actions; the gateway auto-selects a
   healthy reader account under budget (reuse
   `PeerResolver.select_account_for_resolution`, generalised to read budgets).
   Outbound actions keep requiring an explicit `account_id`.

4. **Geo.** Read-only through an RU exit is low-risk, but prefer geo-coherent reader
   accounts anyway; the existing geo gate (`actions.py:95`) can stay as-is for reads
   (RU account + RU proxy = no `CRITICAL`).

5. **Caching.** Add a `chat_info` cache (Redis, TTL ~7d, keyed by username) mirroring
   `peer_cache_*` so re-runs and the resolve→info→history chain don't re-hit Telegram.
   Org enrichment is bursty and re-runnable — caching is the main flood defence.

## 5. Webhook result shapes

No new event types — reads use the existing `task_complete` envelope
(`specs/001-fleet-orchestrator/contracts/webhook-events.md`) with the action's
`result` object from §3. `task_failed` carries `error_code` for the
not-found/private/flood cases (`UsernameNotOccupied`, `ChannelPrivate`,
`FloodWait` → existing `flood_wait` event still fires).

## 6. Out of scope (keep as ban-risk boundary)

- Joining private/closed groups or reading their history (needs membership = action).
- Reading discussion/comment threads (needs join of the linked chat).
- Media download, member enumeration/scraping, forwarding.
- Any write. Outreach (`send_message` cold DM to a resolved `peer_id`) is the
  **separate downstream** layer and stays fully warmup/geo/FIFO-gated.

## 7. Files to touch (P1 = resolve enrich + get_chat_info + get_chat_history)

- `app/api/v1/actions.py` — action enum (`:65`), `GetChatInfoPayload` /
  `GetChatHistoryPayload` models, generalise warmup exemption (`:132`) to
  `READ_ACTIONS`, optional `account_id` for reads.
- `app/workers/get_chat_info.py`, `app/workers/get_chat_history.py` — new workers.
- `app/workers/resolve_username.py` — add `type`/`title`/flags to result.
- `app/workers/arq_settings.py` — register the two new worker functions.
- `app/services/peer_resolver.py` (or a new `read_dispatcher.py`) — generalise
  account auto-selection + per-action read budgets.
- `app/db/redis_client.py` — `chat_info_cache_get/set`.
- `app/db/models.py` — `research_read` use_case (+ enum).
- `config/safety.yaml` / `app/core/safety_defaults.py` — read rate limits +
  `research_read` warmup schedule (gateless).
- `specs/001-fleet-orchestrator/contracts/gateway-api.md` + tests
  (`tests/integration/test_task_dispatch.py`) — document + cover the new actions.

## 8. Phasing

1. **P1** — `resolve_username` enrich + `get_chat_info` + `get_chat_history`, the
   `READ_ACTIONS` exemption, read budgets, chat-info cache. This alone backfills the
   bulk of `social.telegram` + subscriber-scale + post-derived signals.
2. **Integration smoke** — scraper calls fleet `POST /v1/action` (port 8010) for the
   49 known handles, validates the REST+webhook round-trip end-to-end (zero ban-risk).
3. **P2** — `search_public_chat` discovery, only if measured handle-miss rate justifies it.
4. **Downstream (separate)** — `cold_dm` outreach to resolved peers, gated as today.

---

## 9. Second consumer: channel monitoring (Auto-Monitor domain)

**Added 2026-06-17.** A new consumer landed: the `15-Auto-Monitor` job/freelance pipeline
(`own_knowledge_base`, fed by `auto-monitor-ml-cv`). Its **direction #3** is *monitoring
profile Telegram channels/groups* for new job/order posts, then LLM-scoring each post
against Ivan's profile and notifying on a hit. Implementation is **deferred** (TG
reachability in RU is unstable right now) — this section is the design so the fleet is
ready when it's switched on.

### 9.1 Why the §3 actions are not enough

§2–§3 model **one-shot enrichment**: given a handle, read its About + last N posts **once**.
Monitoring is the opposite access pattern:

- **Recurring & incremental** — the same watchlist of M channels is polled every few minutes;
  on each tick we want *only posts newer than last seen*, not the whole tail again.
- **Keyword-filtered at source** — most posts in a job channel are irrelevant; we want the
  Telegram-side search to pre-filter (`#task/detection`, `CV`, `Python`, `ML`, …) instead of
  shipping every post to the LLM.
- **Fan-in across many chats** — one reader account subscribed to N channels should be able to
  surface matches across all of them in one call.

So monitoring needs **cursor-based history**, **in-chat search**, and (ideally) **push instead
of poll**. Scoring/dedup/relevance stays **downstream** (the Auto-Monitor LLM agent); the fleet
stays stateless and just serves reads.

### 9.2 Actions

#### 9.2.1 `get_chat_history` — ENHANCE for incremental polling (P1)

Add cursor params so a monitor fetches only the new tail:

```jsonc
// payload (additions to §3.3)
{ "username": "ml_jobs", "limit": 30,
  "min_id": 4012,        // return only messages with id > min_id (last seen) — the poll cursor
  "offset_id": 0 }       // optional: page backwards from this id for backfill
```
kurigram: `client.get_chat_history(chat_id, limit=limit, offset_id=offset_id)`; drop messages
with `message_id <= min_id` and early-stop. `min_id` is the natural "since" cursor — **the
consumer owns the cursor** (stores last-seen id per channel in the Auto-Monitor DB); the fleet
remains stateless. Without this, every poll re-reads the whole tail and burns the read budget.

#### 9.2.2 `search_messages` — NEW (P1, in-chat keyword search)

Keyword search inside one public chat — pre-filters job channels to relevant posts only.

```jsonc
// payload
{ "username": "ml_jobs", "peer_id": null,
  "query": "computer vision", "limit": 30, "min_date": "2026-06-01" }
```
kurigram: `async for m in client.search_messages(chat_id, query=query, limit=limit): ...`.
Public channels are searchable **without joining**. Result mirrors §3.3 `posts[]`. This is the
workhorse for monitoring: a channel that posts 200 msgs/day yields maybe 2 relevant ones.

#### 9.2.3 `search_global` — NEW (P2, cross-chat fan-in)

Search every dialog the reader account participates in, in one call.

```jsonc
// payload
{ "query": "детекция вакансия", "limit": 50 }
```
kurigram: `client.search_global(query, limit=limit)`. With one reader account joined to the
whole watchlist, this collapses M per-channel polls into one request. Coverage/ranking is
fuzzier than per-chat search → **P2**, ship `search_messages` first and measure.

#### 9.2.4 `get_messages` — NEW (P2, resolve a deep link)

Fetch specific posts by id — e.g. when SERP/web research yields a `t.me/<chat>/<id>` link.

```jsonc
// payload
{ "username": "ml_jobs", "peer_id": null, "message_ids": [4012, 4013] }
```
kurigram: `client.get_messages(chat_id, message_ids=message_ids)`. Read-only; result = `posts[]`.

All four are **read-only on public entities** → add to `READ_ACTIONS` (§4.1), warmup-exempt,
rate-limited. Budgets (per account/day): `search_messages` 150, `search_global` 100,
`get_messages` 200, plus the existing `get_chat_history` 80.

### 9.3 Push instead of poll — extend the watcher (P1 architectural, biggest win)

Polling N channels every few minutes scales the read budget linearly with the watchlist. The
fleet already has a **watcher subsystem** (`app/watchers/watcher_process.py`, US2) that holds
live per-account clients to catch inbound DMs. Extend it to also subscribe to `on_message`
updates for **channels the reader account has joined**, emitting a new `channel_post` webhook
(handle, message_id, date, text, urls). For joined channels this is **near-zero per-post cost
and instant** — far better than `get_chat_history` polling, which then becomes the fallback for
channels not worth joining. The consumer still LLM-scores and dedups downstream.

- New event type `channel_post` in `webhook_events.py` (the §5 "no new event types" rule is
  enrichment-only; monitoring genuinely needs a push event).
- Watcher must filter to the channels on the account's monitor watchlist (a `monitored_chats`
  list per reader account in `app/db/models.py`), not every joined chat.

### 9.4 Joining channels to monitor — behavioural, stays gated

To get push updates (9.3) or to read a closed/discussion chat, the reader must **join**.
Joining is behavioural footprint → **NOT** a `READ_ACTION`; it stays warmup/rate-gated.

- Generalise `join_group` (§4 / existing worker) to accept `username` **or** `invite_link`
  (kurigram `client.join_chat(username_or_link)`). Currently invite-link only.
- **Low daily cap** — joining many channels fast is a classic ban trigger. Cap joins per reader
  account low (e.g. 3–5/day, reuse the `join_groups`/`inviting` budgets) and spread the
  watchlist across the reader pool over days. Public-channel **search/history without joining
  (9.2) is the zero-risk default**; only join channels that justify the push path.

### 9.5 Cross-cutting (monitoring)

1. **Reuse the `research_read` reader pool** (§4.2) — or a sibling `monitor_read` use_case if you
   want monitoring reads isolated from enrichment reads. Both are gateless.
2. **Cursor state lives in the consumer, not the fleet** (`min_id` per channel in the
   Auto-Monitor DB). Fleet stays stateless and re-runnable.
3. **Account-agnostic dispatch** (§4.3) applies to all four read actions; joins keep requiring an
   explicit `account_id` (behavioural, tied to a warmed identity).
4. **Watchlist→reader assignment is sticky** — a given channel should be monitored by a stable
   reader account (so the joined-state and push subscription persist); don't round-robin joins.
5. **RU reachability caveat** — the reason this is deferred: `t.me`/MTProto reachability from RU
   exits is currently unstable. The fleet path (live sessions + geo-coherent RU proxies) is the
   most robust option, but monitoring should degrade gracefully (mark a channel `unreachable`,
   retry with backoff) rather than hammer.

### 9.6 Files to touch (monitoring delta, P1 = history cursor + search_messages + watcher push)

- `app/api/v1/actions.py` — `min_id`/`offset_id` on `GetChatHistoryPayload`; new
  `SearchMessagesPayload` / `SearchGlobalPayload` / `GetMessagesPayload`; add the four to
  `READ_ACTIONS`; generalise `join_group` payload to accept `username`.
- `app/workers/search_messages.py`, `app/workers/get_messages.py` (+ `search_global.py` P2) — new workers.
- `app/workers/get_chat_history.py`, `app/workers/join_group.py` — enhance per above.
- `app/workers/arq_settings.py` — register new workers.
- `app/watchers/watcher_process.py` — `on_message` channel subscription + `channel_post` emit.
- `app/db/models.py` — `monitored_chats` per reader account (+ optional `monitor_read` use_case).
- `app/api/v1/webhook_events.py` — `channel_post` event type.
- `config/safety.yaml` / `app/core/safety_defaults.py` — read budgets above + low join cap for monitoring.

### 9.7 Phasing (monitoring)

1. **M1 (poll MVP)** — `get_chat_history` cursor + `search_messages`, read budgets. Auto-Monitor
   polls a small watchlist via search, LLM-scores hits. Zero joins, zero ban-risk, validates the
   end-to-end monitor→score→notify loop while TG reachability is still shaky.
2. **M2 (push)** — watcher `channel_post` + `join_group` by username, for the channels M1 proved
   worth following. Cuts read volume and adds near-real-time.
3. **M3** — `search_global` fan-in + `get_messages` deep-link resolution, if M1/M2 metrics justify.
