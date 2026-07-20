# Provisioning runbook (M4 — hybrid per-client isolation)

Operate multiple isolated Atomic Engage client instances on one host. Each client gets
its own postgres DB, redis, named volumes, docker network, `API_KEY`, published gateway
port, and a **disjoint puls-proxy port slice**. Design: [`docs/m4-hybrid-isolation.md`](./m4-hybrid-isolation.md).

Everything below runs from `fleet_manager/`.

## Pieces

| File | Role |
|------|------|
| `compose.client.template.yml` | shared, parametrized per-client compose (all values via `${VAR}` from `.env`) |
| `scripts/provision_client.py` | render an instance dir + registry entry; seed its proxy pool |
| `scripts/seed_accounts.py` | import converted accounts into ONE client's DB, assigning that client's proxies |
| `scripts/backup_all.sh` | `pg_dump | gzip` every client DB, `rclone copy` to a remote, 30-day local rotation |
| `instances/<client_id>/` | rendered `docker-compose.yml`, `.env` (mode 600, secrets), `proxies.seed.json` |
| `instances/registry.json` | source of truth: client_id → ports / db name / status / created_at (NO secrets) |

Port ownership: Engage owns puls ports **11100–22000**; give each client a disjoint slice.
postgres/redis are **internal-only** (not published) — reach them via `docker compose exec`
or a one-off `docker compose run`.

## 1. Provision a client

Render only (no DB, no docker, no network — safe to inspect):

```bash
python scripts/provision_client.py \
  --client-id 246903202 --gateway-port 18081 \
  --proxy-ports 11100-11149 --country us --sessttl 30 --render-only
```

This writes `instances/246903202/{docker-compose.yml,.env,proxies.seed.json}` and adds the
client to `instances/registry.json`. Secrets (`POSTGRES_PASSWORD`, `API_KEY`) are generated
into `.env` (never printed in full, never in the registry). Pass `--postgres-password` /
`--api-key` to supply your own, `--n8n-webhook-url` to set the client's webhook, and
`--proxy-type mobile_4g|residential|datacenter`.

Collisions are refused: reused `--client-id`, reused `--gateway-port`, a slice outside
11100–22000, or a slice overlapping another client all abort with a clear message.

Bring the instance up (shared image `atomic-engage:latest` must be built once —
`docker build -t atomic-engage:latest .`):

```bash
cd instances/246903202
docker compose --env-file .env up -d
docker compose --env-file .env ps
curl -f http://localhost:18081/v1/fleet/health
```

Seed the proxy pool against the running gateway (real puls creds from the env, never on
the CLI or on disk):

```bash
export PULS_PROXY_ID=... PULS_PROXY_PASSWORD=...
python scripts/provision_client.py \
  --client-id 246903202 --gateway-port 18081 \
  --proxy-ports 11100-11149 --country us --sessttl 30
```

> The provisioner is idempotent on files but refuses a duplicate registry entry. To
> re-seed proxies for an existing client, POST to `/v1/proxies` directly, or remove the
> registry entry + `instances/<id>/` and re-run. (Proxy URL scheme:
> `http://<id>__cr.<cc>;sessttl.<ttl>:<pass>@np.puls-proxy.com:<port>` — the country is
> parsed back out of the login by `ProxyManager.country_from_login_hint`.)

## 2. Seed accounts into a client

`accounts.json` is produced by `scripts/convert_sessions.py`
(`[{phone, session_string, first_name, last_name, username}, …]`). Because postgres is
internal-only, run the seeder inside the instance network so `postgres:5432` resolves:

```bash
cd instances/246903202
docker compose --env-file .env run --rm \
  -v /abs/path/accounts.json:/tmp/accounts.json \
  gateway python /app/scripts/seed_accounts.py --accounts-json /tmp/accounts.json
```

Each account is assigned a proxy from that client's **reserve** pool (round-robin; the
proxy flips to `active`), an `ApiCredential` (least-loaded), and a fresh device
fingerprint. `phone_country` is derived from the phone number, falling back to the proxy
country. Dry-run first (no DB, no imports): `python scripts/seed_accounts.py
--accounts-json accounts.json --dry-run`.

## 3. List instances

```bash
jq '.instances | keys' instances/registry.json
jq '.instances["246903202"]' instances/registry.json
docker ps --filter "name=-246903202"
```

## 4. Backups

`scripts/backup_all.sh` iterates the registry, `pg_dump | gzip` each running client DB to
`$BACKUP_ROOT/<client_id>/<timestamp>.sql.gz`, `rclone copy` the tree to `$RCLONE_REMOTE`,
then prunes local dumps older than `$RETENTION_DAYS` (default 30). The remote is the
long-term archive (no auto-prune).

One-time (Ivan): `rclone config` to create the remote (Google Drive OAuth is a manual
browser step). Then:

```bash
RCLONE_REMOTE=gdrive-engage:/backups BACKUP_ROOT=/backups RETENTION_DAYS=30 \
  ./scripts/backup_all.sh
```

Leave `RCLONE_REMOTE` empty for local-only dumps. Cron (2 AM daily):

```cron
0 2 * * * RCLONE_REMOTE=gdrive-engage:/backups /opt/atomic-engage/fleet_manager/scripts/backup_all.sh
```

Restore: see [`docs/m4-hybrid-isolation.md`](./m4-hybrid-isolation.md) §4.5.

## 5. Tear down a client

```bash
cd instances/<client_id>
docker compose --env-file .env down            # keep data (volumes remain)
docker compose --env-file .env down -v         # DESTRUCTIVE: also drops postgres/redis volumes
```

Then remove `instances/<client_id>/` and delete the client's entry from
`instances/registry.json` (e.g. `jq 'del(.instances["<client_id>"])' …`). Back up first.

## The two real clients

Shared box, disjoint slices:

```bash
# Client A
python scripts/provision_client.py --client-id 246903202 \
  --gateway-port 18081 --proxy-ports 11100-11149 --country us --sessttl 30 --render-only
# Client B
python scripts/provision_client.py --client-id 247147941 \
  --gateway-port 18082 --proxy-ports 11150-11199 --country us --sessttl 30 --render-only
```

Drop `--render-only` (with `PULS_PROXY_ID` / `PULS_PROXY_PASSWORD` exported and the
instance up) to also seed each proxy pool. Adjust `--country` / `--sessttl` per client.
