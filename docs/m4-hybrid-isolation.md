---
created: 2026-07-16
status: draft
domain: in-work
confidence_level: high
tags: [type/runbook, ops/isolation, ops/backup, ops/provisioning]
---

# M4: Atomic Engage — Hybrid Per-Client Isolation, Provisioning & Backups

**Purpose:** Operational design and runbook for managing multiple isolated Atomic Engage client instances on a single AlphaVPS box, including provisioning, per-client proxy configuration, automated backups to Google Drive, and verification of tenant isolation.

---

## 1. Hybrid Per-Client Isolation Model

### 1.1 Isolation Strategy

Each paying client runs their own **isolated instance** of Atomic Engage on the shared AlphaVPS server. A single client's operational incident (e.g., account flood-limit by Telegram, banned accounts, rate-limiting) **must not** affect other clients' data, uptime, or accounts.

**Approach:** Shared Docker image (built once, no per-client forks unless custom features required) + per-client `docker-compose.yml` with:
- Separate PostgreSQL database (user, password, schema)
- Separate Redis instance
- Separate named volumes (postgres_data, redis_data)
- Unique `API_KEY` per client
- Port allocation per client (gateway on unique `HOST_PORT`, postgres on unique `CONTAINER_PORT`)

[ASSUMPTION] Custom features for a client require a forked copy of the repo only if they cannot be feature-flagged within the shared image; most clients use the standard image.

### 1.2 Operational Ceiling

**~100 accounts per instance** (from plan notes). When a client approaches this limit, scaling decision: upgrade RAM/CPU on existing box, or provision a second instance for that client on a different server.

### 1.3 Concrete Deployment Layout

```
/opt/atomic-engage/
├── image/                           # shared Docker image (built once)
│   └── <hash>/Dockerfile, src/, etc.
└── instances/
    ├── client-001/                  # Client A (ABC Corp)
    │   ├── docker-compose.yml       # per-client override
    │   ├── .env.prod                # per-client secrets (DATABASE_URL, API_KEY, etc.)
    │   └── volumes/                 # persistent data
    │       ├── postgres_data/
    │       └── redis_data/
    ├── client-002/                  # Client B (XYZ LLC)
    │   ├── docker-compose.yml
    │   ├── .env.prod
    │   └── volumes/
    ├── registry.json                # instance registry (source of truth)
    └── provisioner.sh               # deployment automation
```

---

## 2. Provisioner: Deploy Client N

### 2.1 Provisioner Input Specification

When deploying a new client, gather:

| Input | Type | Example | Purpose |
|-------|------|---------|---------|
| `CLIENT_ID` | string (kebab-case) | `client-001` | Unique identifier (folder name, DNS label) |
| `CLIENT_NAME` | string | `ABC Marketing Corp` | Human-readable label for logs/alerts |
| `GATEWAY_PORT` | int (8000–8999) | `8001` | Host port; must not collide with existing clients |
| `POSTGRES_PORT` | int (5430–5999) | `5435` | Host port for PostgreSQL (optional; can be internal-only) |
| `API_KEY` | string (UUID or random secret) | `a1b2c3d4-...` | Unique per-client auth token |
| `PROXIES` | JSON array or CSV | `[{url: "...", country: "US"}, ...]` | Initial proxy list (see §2.4) |
| `POSTGRES_USER` | string | `client_001_user` | Database user (auto-generated from `CLIENT_ID`) |
| `POSTGRES_PASSWORD` | string (strong) | `<random>` | Database password (generated, stored in `.env.prod`) |
| `POSTGRES_DB` | string | `client_001_db` | Database name (auto-generated from `CLIENT_ID`) |

### 2.2 Provisioner Workflow

```
1. Validate inputs (CLIENT_ID not in registry, GATEWAY_PORT unused)
2. Generate secrets (POSTGRES_PASSWORD, API_KEY if not supplied)
3. Create /opt/atomic-engage/instances/client-NNN/ directory
4. Render docker-compose.yml from template (substitute ports, DB/Redis URLs)
5. Create .env.prod with DATABASE_URL, REDIS_URL, API_KEY, etc.
6. Run 'docker compose build' (if custom fork; otherwise skip — reuse shared image)
7. Run 'docker compose up -d' (starts gateway, workers, watchers, postgres, redis)
8. Wait for postgres:5432 ready (pg_isready) and gateway /health 200 OK
9. Seed proxies table from PROXIES input (POST /v1/proxies for each proxy)
10. Record instance in registry.json (CLIENT_ID, GATEWAY_PORT, POSTGRES_PORT, status, created_at)
11. Return summary: CLIENT_ID, gateway URL http://localhost:GATEWAY_PORT, provisioner log
```

### 2.3 Docker Compose Template (per-client)

**Template file:** `/opt/atomic-engage/instances/.compose.template.yml`

```yaml
version: "3.8"

services:
  gateway:
    image: atomic-engage:latest  # shared image tag
    container_name: gateway-{{CLIENT_ID}}
    ports:
      - "{{GATEWAY_PORT}}:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://{{POSTGRES_USER}}:{{POSTGRES_PASSWORD}}@postgres:5432/{{POSTGRES_DB}}
      - REDIS_URL=redis://redis:6379
      - API_KEY={{API_KEY}}
      - N8N_SYSTEM_WEBHOOK_URL={{N8N_WEBHOOK_URL}}
      - GEOIP_CITY_DB_PATH=/app/GeoLite2-City.mmdb
      - GEOIP_ASN_DB_PATH=/app/GeoLite2-ASN.mmdb
    depends_on:
      - postgres
      - redis
    volumes:
      - ./GeoLite2-City.mmdb:/app/GeoLite2-City.mmdb:ro
      - ./GeoLite2-ASN.mmdb:/app/GeoLite2-ASN.mmdb:ro
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  worker_1:
    image: atomic-engage:latest
    container_name: worker_1-{{CLIENT_ID}}
    command: python -m arq app.workers.arq_settings.WorkerSettings
    environment:
      - DATABASE_URL=postgresql+asyncpg://{{POSTGRES_USER}}:{{POSTGRES_PASSWORD}}@postgres:5432/{{POSTGRES_DB}}
      - REDIS_URL=redis://redis:6379
      - API_KEY={{API_KEY}}
      - N8N_SYSTEM_WEBHOOK_URL={{N8N_WEBHOOK_URL}}
      - GEOIP_CITY_DB_PATH=/app/GeoLite2-City.mmdb
      - GEOIP_ASN_DB_PATH=/app/GeoLite2-ASN.mmdb
    depends_on:
      - postgres
      - redis
    volumes:
      - ./GeoLite2-City.mmdb:/app/GeoLite2-City.mmdb:ro
      - ./GeoLite2-ASN.mmdb:/app/GeoLite2-ASN.mmdb:ro
    restart: unless-stopped

  worker_2:
    image: atomic-engage:latest
    container_name: worker_2-{{CLIENT_ID}}
    command: python -m arq app.workers.arq_settings.WorkerSettings
    environment:
      - DATABASE_URL=postgresql+asyncpg://{{POSTGRES_USER}}:{{POSTGRES_PASSWORD}}@postgres:5432/{{POSTGRES_DB}}
      - REDIS_URL=redis://redis:6379
      - API_KEY={{API_KEY}}
      - N8N_SYSTEM_WEBHOOK_URL={{N8N_WEBHOOK_URL}}
      - GEOIP_CITY_DB_PATH=/app/GeoLite2-City.mmdb
      - GEOIP_ASN_DB_PATH=/app/GeoLite2-ASN.mmdb
    depends_on:
      - postgres
      - redis
    volumes:
      - ./GeoLite2-City.mmdb:/app/GeoLite2-City.mmdb:ro
      - ./GeoLite2-ASN.mmdb:/app/GeoLite2-ASN.mmdb:ro
    restart: unless-stopped

  watcher_1:
    image: atomic-engage:latest
    container_name: watcher_1-{{CLIENT_ID}}
    command: python -m app.watchers.watcher_process
    environment:
      - DATABASE_URL=postgresql+asyncpg://{{POSTGRES_USER}}:{{POSTGRES_PASSWORD}}@postgres:5432/{{POSTGRES_DB}}
      - REDIS_URL=redis://redis:6379
      - API_KEY={{API_KEY}}
      - N8N_SYSTEM_WEBHOOK_URL={{N8N_WEBHOOK_URL}}
    depends_on:
      - postgres
      - redis
    restart: unless-stopped

  # watcher_2, watcher_3, watcher_4 — same pattern, incremented container_name

  postgres:
    image: postgres:15
    container_name: postgres-{{CLIENT_ID}}
    environment:
      - POSTGRES_USER={{POSTGRES_USER}}
      - POSTGRES_PASSWORD={{POSTGRES_PASSWORD}}
      - POSTGRES_DB={{POSTGRES_DB}}
    volumes:
      - postgres_data-{{CLIENT_ID}}:/var/lib/postgresql/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {{POSTGRES_USER}}"]
      interval: 10s
      timeout: 5s
      retries: 3

  redis:
    image: redis:7
    container_name: redis-{{CLIENT_ID}}
    volumes:
      - redis_data-{{CLIENT_ID}}:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  postgres_data-{{CLIENT_ID}}:
    driver: local
  redis_data-{{CLIENT_ID}}:
    driver: local

networks:
  default:
    name: fleet_network-{{CLIENT_ID}}
```

### 2.4 Instance Registry Schema

**File:** `/opt/atomic-engage/instances/registry.json`

```json
{
  "instances": [
    {
      "client_id": "client-001",
      "client_name": "ABC Marketing Corp",
      "gateway_port": 8001,
      "postgres_port": 5435,
      "postgres_user": "client_001_user",
      "postgres_db": "client_001_db",
      "api_key": "a1b2c3d4-...",
      "status": "running",
      "created_at": "2026-07-16T10:30:00Z",
      "last_sync_proxies_at": "2026-07-16T10:35:00Z",
      "accounts_count": 42,
      "proxies_count": 8,
      "notes": "Baseline client, 5 Telegram accounts per user"
    },
    {
      "client_id": "client-002",
      "client_name": "XYZ LLC",
      "gateway_port": 8002,
      "postgres_port": 5436,
      "postgres_user": "client_002_user",
      "postgres_db": "client_002_db",
      "api_key": "e5f6g7h8-...",
      "status": "running",
      "created_at": "2026-07-16T11:15:00Z",
      "last_sync_proxies_at": "2026-07-16T11:20:00Z",
      "accounts_count": 98,
      "proxies_count": 12,
      "notes": "Approaching ceiling; monitor for scaling"
    }
  ],
  "last_updated": "2026-07-16T11:20:00Z"
}
```

---

## 3. Per-Client Proxy Configuration

### 3.1 Proxy Model & API

**Database model** (`fleet_manager/app/db/models.py`):

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | BigInteger (PK) | No | Proxy internal ID (auto-increment) |
| `url` | String(500) | No | Proxy endpoint (e.g. `http://user:pass@1.2.3.4:8080`) |
| `proxy_type` | String(20) | No | Type: `mobile_4g`, `residential`, `datacenter` |
| `country` | String(2) | No | 2-letter ISO country code (resolved via GeoIP or hint) |
| `asn` | Integer | Yes | Autonomous System Number (from MaxMind GeoLite2-ASN) |
| `tz_offset` | Integer | No | UTC offset in minutes (default 0); used for account action scheduling |
| `state` | String(20) | No | State: `reserve` (not yet assigned) or `active` (in use by accounts) |
| `is_healthy` | Boolean | No | Health flag (True initially; set False if proxy fails health check) |
| `created_at` | DateTime | No | Timestamp (server default: now) |

**Account references proxy:** Foreign key `Account.proxy_id → Proxy.id` (one proxy can serve multiple accounts; **but proxies are per-client only** — different instances have isolated proxy tables).

### 3.2 Proxies API (Per-Client)

All endpoints require valid `API_KEY` header (verified per instance):

#### POST `/v1/proxies` — Create Proxy

**Request:**
```json
{
  "url": "http://user:pass@1.2.3.4:8080",
  "proxy_type": "residential",
  "country": "US",
  "tz_offset": -300
}
```

**Parameters:**
- `url` (required): Proxy endpoint URL
- `proxy_type` (optional, default `residential`): One of `mobile_4g`, `residential`, `datacenter`
- `country` (optional): 2-letter country code; if omitted, resolved via GeoIP on proxy host
- `tz_offset` (optional, default 0): UTC offset in minutes

**Response (201):**
```json
{
  "proxy_id": 1,
  "host": "1.2.3.4",
  "country": "US",
  "proxy_type": "residential",
  "state": "reserve",
  "is_healthy": true
}
```

#### GET `/v1/proxies/{proxy_id}` — Fetch Proxy Details

**Response (200):**
```json
{
  "proxy_id": 1,
  "country": "US",
  "proxy_type": "residential",
  "state": "reserve",
  "is_healthy": true
}
```

### 3.3 Provisioner: Seed Proxies on Deployment

After instance is running (§2.2, step 8), the provisioner seeds the client's proxy pool:

```python
# Pseudocode: provisioner.py
import requests

proxies_input = [
    {"url": "http://user1:pass1@proxy1.com:8080", "proxy_type": "residential", "country": "US", "tz_offset": -300},
    {"url": "http://user2:pass2@proxy2.com:8080", "proxy_type": "mobile_4g", "country": "BR", "tz_offset": -180},
    # ... more proxies
]

for proxy_spec in proxies_input:
    response = requests.post(
        f"http://localhost:{GATEWAY_PORT}/v1/proxies",
        json=proxy_spec,
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    assert response.status_code == 201, f"Proxy creation failed: {response.text}"
    print(f"Seeded proxy_id={response.json()['proxy_id']}")

# Log: "Seeded N proxies for {CLIENT_ID}"
```

**Key isolation guarantee:** Each instance has its own PostgreSQL DB → proxies table is separate per client. Instance `client-001` can never query or access `client-002`'s proxies, even if they share the same Docker image.

---

## 4. Automated Backups to Google Drive

### 4.1 Backup Strategy

- **Frequency:** Daily, 2 AM UTC (cron job on AlphaVPS host)
- **Scope:** All PostgreSQL databases (one dump per client instance)
- **Transport:** Local → `/backups` (mounted NFS or local FS) → Google Drive (via rclone)
- **Retention:** Last 30 days (older backups auto-deleted locally; Google Drive = archive)
- **Restore path:** Download from Google Drive → `pg_restore` on target instance

### 4.2 Backup Cron Job (Host Level)

**File:** `/opt/atomic-engage/scripts/backup-all-instances.sh`

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/backups/atomic-engage"
INSTANCES_REGISTRY="/opt/atomic-engage/instances/registry.json"
RETENTION_DAYS=30
LOG_FILE="/var/log/atomic-engage-backup.log"

mkdir -p "$BACKUP_DIR"

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "Starting backup of all Atomic Engage instances..."

# Parse registry.json and iterate clients
jq -r '.instances[] | select(.status == "running") | "\(.client_id)|\(.postgres_user)|\(.postgres_db)|\(.gateway_port)"' "$INSTANCES_REGISTRY" | while IFS='|' read -r client_id pg_user pg_db gateway_port; do
  
  log "Backing up $client_id (DB: $pg_db, port: $gateway_port)..."
  
  # Dump PostgreSQL (inside container, via docker exec)
  CONTAINER_NAME="postgres-${client_id}"
  DUMP_FILE="${BACKUP_DIR}/${client_id}-$(date +%Y%m%d-%H%M%S).sql.gz"
  
  docker exec "$CONTAINER_NAME" pg_dump -U "$pg_user" "$pg_db" \
    | gzip > "$DUMP_FILE"
  
  if [ $? -eq 0 ]; then
    DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
    log "✓ Backup created: $DUMP_FILE ($DUMP_SIZE)"
  else
    log "✗ Backup FAILED for $client_id"
    continue
  fi
done

log "Uploading backups to Google Drive via rclone..."
rclone copy "$BACKUP_DIR" "gdrive-atomic-engage:/backups/atomic-engage" \
  --exclude "*.tmp" --update --verbose 2>&1 | tee -a "$LOG_FILE"

if [ $? -eq 0 ]; then
  log "✓ Backup uploaded to Google Drive"
else
  log "✗ rclone upload FAILED"
  exit 1
fi

log "Rotating old backups (>$RETENTION_DAYS days)..."
find "$BACKUP_DIR" -name "*.sql.gz" -type f -mtime "+$RETENTION_DAYS" -exec rm -v {} \; | tee -a "$LOG_FILE"

log "Backup cycle complete."
```

**Cron entry** (as root or dedicated backup user):

```cron
# /etc/cron.d/atomic-engage-backup
0 2 * * * /opt/atomic-engage/scripts/backup-all-instances.sh
```

### 4.3 rclone Configuration (One-Time Setup)

**Prerequisites:**
- rclone installed on AlphaVPS: `curl https://rclone.org/install.sh | bash`
- Google Drive OAuth app (Google Cloud Console)

**OAuth Setup (Ivan's manual step):**

```bash
rclone authorize drive "your-client-id" "your-client-secret"
# Opens browser → consent screen → returns auth token
# Copy token into rclone.conf
```

**rclone.conf entry:**

```
[gdrive-atomic-engage]
type = drive
client_id = <your-client-id>
client_secret = <your-client-secret>
token = <oauth-token-json>
root_folder_id = <folder-id>  # Google Drive folder ID for /backups
```

**Verify rclone works:**

```bash
rclone ls gdrive-atomic-engage:/backups/atomic-engage
```

### 4.4 Backup Rotation & Retention

- **Local:** Keep 30 days; older files auto-deleted by cron job (§4.2, `find ... -mtime "+$RETENTION_DAYS"`)
- **Google Drive:** No auto-delete; manual archival/cleanup (Ivan's decision). Recommended: quarterly review, move backups >90 days to "Archive" folder.

### 4.5 Restore Runbook

**Scenario:** Client `client-001` lost data; need to restore from backup dated 2026-07-14.

**Steps:**

1. **Identify backup file:**
   ```bash
   # Local backups
   ls -lah /backups/atomic-engage/ | grep "client-001"
   
   # Or download from Google Drive
   rclone copy gdrive-atomic-engage:/backups/atomic-engage/client-001-20260714-*.sql.gz /tmp/
   ```

2. **Stop the instance (optional, but recommended for consistency):**
   ```bash
   cd /opt/atomic-engage/instances/client-001
   docker compose down
   ```

3. **Backup current DB (safety):**
   ```bash
   docker exec postgres-client-001 pg_dump -U client_001_user client_001_db \
     | gzip > /backups/atomic-engage/client-001-BACKUP-BEFORE-RESTORE-$(date +%Y%m%d-%H%M%S).sql.gz
   ```

4. **Restore from dump:**
   ```bash
   # Start postgres only (if down), or connect to running instance
   cd /opt/atomic-engage/instances/client-001
   docker compose up -d postgres redis  # if stopped
   
   # Drop & recreate DB (destructive; confirm!)
   docker exec postgres-client-001 psql -U client_001_user -d postgres -c "DROP DATABASE IF EXISTS client_001_db;"
   docker exec postgres-client-001 psql -U client_001_user -d postgres -c "CREATE DATABASE client_001_db;"
   
   # Restore dump
   gunzip < /backups/atomic-engage/client-001-20260714-235959.sql.gz | \
     docker exec -i postgres-client-001 psql -U client_001_user -d client_001_db
   ```

5. **Verify restoration:**
   ```bash
   # Check row counts
   docker exec postgres-client-001 psql -U client_001_user -d client_001_db -c "SELECT COUNT(*) FROM accounts;"
   docker exec postgres-client-001 psql -U client_001_user -d client_001_db -c "SELECT COUNT(*) FROM proxies;"
   ```

6. **Restart instance:**
   ```bash
   cd /opt/atomic-engage/instances/client-001
   docker compose up -d
   curl http://localhost:8001/health  # confirm gateway responsive
   ```

7. **Log and notify:**
   ```bash
   echo "RESTORED client-001 from 2026-07-14 backup" >> /var/log/atomic-engage-backup.log
   # Notify ops / send Slack alert (if integrated)
   ```

---

## 5. Verification Checklist (M4 Criteria)

### 5.1 Pre-Deployment Verification

- [ ] Docker image builds cleanly: `cd /opt/atomic-engage && docker build -t atomic-engage:latest .`
- [ ] `.env.example` documents all required variables
- [ ] `docker-compose.template.yml` contains all template substitution markers (`{{...}}`)
- [ ] `provisioner.sh` script is executable and syntactically valid
- [ ] `registry.json` is valid JSON (can be parsed by jq/Python)

### 5.2 Post-Deployment Verification (Per-Instance)

For a newly provisioned client instance (`client-001`):

- [ ] **Gateway health:**
  ```bash
  curl http://localhost:8001/health
  # Expected: HTTP 200 OK with JSON body {"status": "ok"}
  ```

- [ ] **Database isolation (critical):**
  ```bash
  # From host
  docker exec postgres-client-001 psql -U client_001_user -d client_001_db -c "SELECT COUNT(*) FROM accounts;" -c "SELECT COUNT(*) FROM proxies;"
  
  # Should return counts; should NOT error (isolation working)
  # Attempt to connect to client-002's DB from client-001 container — should FAIL
  docker exec postgres-client-001 psql -U client_001_user -d client_002_db -c "SELECT 1;" 2>&1 | grep -q "does not exist"
  # Expected: "database \"client_002_db\" does not exist" error ✓
  ```

- [ ] **Redis isolation:**
  ```bash
  docker exec redis-client-001 redis-cli -n 0 PING
  # Expected: PONG
  
  # Confirm separate Redis instance (different port or container)
  docker ps | grep "redis-client"
  ```

- [ ] **API_KEY isolation (requests to different instances must use different keys):**
  ```bash
  # Attempt request to client-001 with wrong key
  curl -H "Authorization: Bearer wrong-key-123" http://localhost:8001/fleet/status
  # Expected: HTTP 401/403 Unauthorized
  
  # Correct key
  curl -H "Authorization: Bearer <API_KEY_CLIENT_001>" http://localhost:8001/fleet/status
  # Expected: HTTP 200 OK with fleet status
  ```

- [ ] **Proxies table seeding:**
  ```bash
  curl -H "Authorization: Bearer <API_KEY_CLIENT_001>" \
    http://localhost:8001/v1/proxies/1
  # Expected: HTTP 200 OK with proxy details (if seed completed)
  # OR HTTP 404 if no proxies exist yet (acceptable, depends on input)
  ```

### 5.3 Multi-Client Isolation Verification (Critical)

With two instances running (`client-001` on port 8001, `client-002` on port 8002):

- [ ] **Accounts table isolation:**
  ```bash
  # Client-001 inserts 10 test accounts
  for i in {1..10}; do
    curl -X POST -H "Authorization: Bearer <API_001>" \
      http://localhost:8001/v1/accounts \
      -d '{"phone": "+1234567890'$i'", ...}'
  done
  
  # Verify client-001 sees 10 accounts
  docker exec postgres-client-001 psql -U client_001_user -d client_001_db \
    -c "SELECT COUNT(*) FROM accounts;"
  # Expected: 10
  
  # Verify client-002 DB is empty (no cross-contamination)
  docker exec postgres-client-002 psql -U client_002_user -d client_002_db \
    -c "SELECT COUNT(*) FROM accounts;"
  # Expected: 0
  ```

- [ ] **Proxies table isolation:**
  ```bash
  # Client-001 seeds 5 proxies
  for i in {1..5}; do
    curl -X POST -H "Authorization: Bearer <API_001>" \
      http://localhost:8001/v1/proxies \
      -d '{"url": "http://proxy'$i'.com:8080", "country": "US"}'
  done
  
  # Client-001 sees 5 proxies
  docker exec postgres-client-001 psql -U client_001_user -d client_001_db \
    -c "SELECT COUNT(*) FROM proxies;"
  # Expected: 5
  
  # Client-002 sees 0 proxies (even though using same shared image)
  docker exec postgres-client-002 psql -U client_002_user -d client_002_db \
    -c "SELECT COUNT(*) FROM proxies;"
  # Expected: 0
  ```

- [ ] **Cross-instance account bleed (MUST fail):**
  ```bash
  # Attempt to access client-002's account via client-001's API
  # (Requires custom test; guard in deps.py API_KEY resolution prevents this)
  # If implementation is correct, client-001 cannot fetch client-002's accounts
  ```

### 5.4 Backup Verification

- [ ] **Local backup created:**
  ```bash
  ls -lah /backups/atomic-engage/ | grep "client-001"
  # Expected: At least one .sql.gz file with size > 0
  ```

- [ ] **Google Drive upload:**
  ```bash
  rclone ls gdrive-atomic-engage:/backups/atomic-engage | grep "client-001"
  # Expected: File listing includes client-001 backups
  ```

- [ ] **Restore works (test on staging):**
  - Create test instance `client-test`
  - Restore from client-001 backup
  - Verify account counts match original
  - Verify proxies table matches original
  - [ ] Restoration succeeds without error

- [ ] **Backup retention working:**
  ```bash
  # Manually create old backup (set atime to 31 days ago)
  touch -t 202606XX /backups/atomic-engage/client-001-OLD.sql.gz
  
  # Run cron script
  /opt/atomic-engage/scripts/backup-all-instances.sh
  
  # Verify old file deleted
  ls /backups/atomic-engage/client-001-OLD.sql.gz 2>&1 | grep "cannot access"
  # Expected: file not found
  ```

---

## 6. Operational Decisions & Open Questions

### For Ivan to Decide

1. **Google Drive OAuth:** Requires manual `rclone authorize` step (§4.3). Can be done once and committed to `.env.prod` (private repo). **Decision:** Who manages Google Drive folder? (Ivan directly, or shared service account?)

2. **Custom client repos:** Most clients use shared image. If a client needs features not in trunk (e.g., custom webhook format), fork or feature-flag? **Current plan:** Fork-copy repo only for heavily custom clients; flag others inline.

3. **Scaling trigger:** At what account count per instance should Ivan spin up a second instance for a client? **Current plan:** ~80–90 accounts (comfort before 100 ceiling).

4. **Backup cadence:** Daily at 2 AM UTC; does this align with Ivan's timezone/maintenance windows? **Adjust cron if needed.**

5. **Monitor/alert:** Is backup failure ('X' in logs) sent to Slack/email? **Current setup:** Logs written to `/var/log/atomic-engage-backup.log`; requires separate monitoring integration.

---

## References

- **Plan source:** `~/.claude/plans/parallel-noodling-bumblebee.md` (M4 section)
- **Repo:** `atomic-brand/atomic-engage` (prods; public)
- **Docker Compose:** `fleet_manager/docker-compose.yml`
- **Proxy model:** `fleet_manager/app/db/models.py` (Proxy class, lines 97–113)
- **Proxies API:** `fleet_manager/app/api/v1/proxies.py`
- **CLAUDE.md:** `atomic-brand/atomic-engage/CLAUDE.md` (architecture overview)
