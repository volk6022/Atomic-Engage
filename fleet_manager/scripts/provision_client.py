#!/usr/bin/env python3
"""Provision an isolated per-client Atomic Engage instance (M4 hybrid isolation).

Given a client id, a gateway host port and a DISJOINT puls-proxy port slice, this:

  (a) renders ``compose.client.template.yml`` + a per-client ``.env`` into
      ``instances/<client_id>/`` (secrets generated, never taken from the CLI unless
      explicitly supplied);
  (b) writes/updates ``instances/registry.json`` (client_id -> ports / db name /
      status / created_at); the registry holds NO secrets;
  (c) seeds the client's ``proxies`` table from the port slice — one Proxy row per
      port — by POSTing to the running instance's ``/v1/proxies`` endpoint (reuses the
      real API + ProxyManager country logic, no duplicated DB writes here).

The puls-proxy account is SHARED across clients; only the port slice, country and
session TTL differ per client. Real proxy credentials (``PULS_PROXY_ID`` /
``PULS_PROXY_PASSWORD``) are read from the environment at seed time and are NEVER
written to disk or the registry — the rendered ``proxies.seed.json`` keeps the
password as a ``${PULS_PROXY_PASSWORD}`` placeholder.

Static build note: ``--render-only`` produces every file WITHOUT touching a database,
docker, or the network, so the output can be verified offline. The live proxy seed and
the real ``docker compose up`` are a separate acceptance step.

Proxy URL scheme (puls-proxy, shared account)::

    http://<id>__cr.<cc>;sessttl.<ttl>:<pass>@np.puls-proxy.com:<port>

Engage owns puls ports 11100-22000; each client gets a disjoint slice.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────
ENGAGE_PROXY_PORT_MIN = 11100
ENGAGE_PROXY_PORT_MAX = 22000
PULS_PROXY_HOST = "np.puls-proxy.com"
PULS_PASSWORD_PLACEHOLDER = "${PULS_PROXY_PASSWORD}"
PULS_ID_PLACEHOLDER = "${PULS_PROXY_ID}"

FLEET_ROOT = Path(__file__).resolve().parent.parent  # fleet_manager/
DEFAULT_TEMPLATE = FLEET_ROOT / "compose.client.template.yml"
DEFAULT_INSTANCES_DIR = FLEET_ROOT / "instances"


# ── helpers ──────────────────────────────────────────────────────────────────
def slugify_db_ident(client_id: str) -> str:
    """PostgreSQL identifiers can't start with a digit; client ids often do.

    ``246903202`` -> ``client_246903202`` ; ``ABC Corp`` -> ``client_abc_corp``.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", client_id.lower()).strip("_")
    return f"client_{cleaned}"


def parse_port_range(spec: str) -> tuple[int, int]:
    """Parse ``11100-11149`` into ``(11100, 11149)`` inclusive."""
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", spec)
    if not m:
        raise ValueError(f"invalid --proxy-ports {spec!r}; expected START-END")
    start, end = int(m.group(1)), int(m.group(2))
    if start > end:
        raise ValueError(f"--proxy-ports start {start} > end {end}")
    return start, end


def load_registry(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "engage_proxy_port_pool": [ENGAGE_PROXY_PORT_MIN, ENGAGE_PROXY_PORT_MAX],
        "instances": {},
        "last_updated": None,
    }


def ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def validate_allocation(
    registry: dict,
    client_id: str,
    gateway_port: int,
    proxy_range: tuple[int, int],
    allow_existing: bool = False,
) -> None:
    """Refuse client-id reuse, gateway-port reuse, out-of-pool or overlapping slices.

    ``allow_existing`` is for the seed-only pass over a client that was already
    rendered: there the entry is expected to be present, and its own port and
    slice must not be read as collisions with itself.
    """
    if proxy_range[0] < ENGAGE_PROXY_PORT_MIN or proxy_range[1] > ENGAGE_PROXY_PORT_MAX:
        raise SystemExit(
            f"proxy slice {proxy_range[0]}-{proxy_range[1]} outside Engage pool "
            f"{ENGAGE_PROXY_PORT_MIN}-{ENGAGE_PROXY_PORT_MAX}"
        )
    instances = registry.get("instances", {})
    if client_id in instances and not allow_existing:
        raise SystemExit(
            f"client_id {client_id!r} already in registry; refusing to overwrite "
            f"(delete its instances/{client_id}/ dir + registry entry to re-provision)"
        )
    for other_id, meta in instances.items():
        if other_id == client_id:
            continue
        if meta.get("gateway_port") == gateway_port:
            raise SystemExit(
                f"gateway_port {gateway_port} already used by {other_id!r}"
            )
        other_range = tuple(meta.get("proxy_port_range", [0, 0]))
        if ranges_overlap(proxy_range, other_range):
            raise SystemExit(
                f"proxy slice {proxy_range[0]}-{proxy_range[1]} overlaps {other_id!r} "
                f"slice {other_range[0]}-{other_range[1]}"
            )


def build_proxy_url(
    puls_id: str, cc: str, sessttl: int, password: str, port: int
) -> str:
    """http://<id>__cr.<cc>;sessttl.<ttl>:<pass>@np.puls-proxy.com:<port>"""
    return (
        f"http://{puls_id}__cr.{cc.lower()};sessttl.{sessttl}:"
        f"{password}@{PULS_PROXY_HOST}:{port}"
    )


def build_proxy_specs(proxy_range: tuple[int, int], args) -> list[dict]:
    """One proxy row per port in the slice; the password stays a placeholder."""
    return [
        {
            "port": port,
            "country": args.country.upper(),
            "proxy_type": args.proxy_type,
            "tz_offset": args.tz_offset,
            "sessttl": args.sessttl,
            "url": build_proxy_url(
                PULS_ID_PLACEHOLDER, args.country, args.sessttl,
                PULS_PASSWORD_PLACEHOLDER, port,
            ),
        }
        for port in range(proxy_range[0], proxy_range[1] + 1)
    ]


def read_env_value(env_path: Path, key: str) -> str | None:
    """Read one KEY=value back out of a rendered instance .env."""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def render_env_file(env_path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Per-client Atomic Engage instance secrets — DO NOT COMMIT (mode 600).",
        "# Consumed by `docker compose --env-file .env -f docker-compose.yml ...`.",
        "",
    ]
    for key, val in values.items():
        lines.append(f"{key}={val}")
    lines += [
        "",
        "# --- puls-proxy shared-account creds (NOT written here) ---",
        f"PULS_PROXY_HOST={PULS_PROXY_HOST}",
        "# PULS_PROXY_ID / PULS_PROXY_PASSWORD are read from the shell environment",
        "# by provision_client.py at proxy-seed time; keep them out of this file.",
        "",
    ]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    # 600 intent: owner read/write only. No-op / best-effort on Windows.
    try:
        os.chmod(env_path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _provision_geoip(client_dir: Path, geoip_src: str) -> None:
    """Provision GeoLite mmdb files into the instance directory.

    If real mmdb files exist in geoip_src, copy them. Otherwise, create 0-byte
    placeholder files so bind-mounts work (app gracefully degrades without valid mmdb).
    Safe to re-run: only creates placeholders if target missing or is a directory.
    """
    geoip_src_path = Path(geoip_src)
    mmdb_files = ["GeoLite2-City.mmdb", "GeoLite2-ASN.mmdb"]

    for mmdb_name in mmdb_files:
        target_path = client_dir / mmdb_name
        src_path = geoip_src_path / mmdb_name

        # If target is an existing file, leave it alone (already provisioned).
        if target_path.is_file():
            continue

        # If target is a directory (from a prior buggy run), remove it.
        if target_path.is_dir():
            import shutil as shutil_module
            shutil_module.rmtree(target_path)

        # Copy from source if it exists, otherwise create a 0-byte placeholder.
        if src_path.is_file():
            shutil.copy2(src_path, target_path)
        else:
            target_path.touch()

    # Check if we created placeholders and warn.
    city_is_placeholder = target_path.stat().st_size == 0 if (client_dir / "GeoLite2-City.mmdb").exists() else False
    asn_is_placeholder = (client_dir / "GeoLite2-ASN.mmdb").stat().st_size == 0 if (client_dir / "GeoLite2-ASN.mmdb").exists() else False

    if city_is_placeholder or asn_is_placeholder:
        print(
            "\n[WARNING] GeoLite mmdb files missing or 0-byte placeholder. "
            "Geo-matching and datacenter-ASN blocking are DISABLED.\n"
            f"  To enable: place real GeoLite files at:\n"
            f"    {client_dir}/GeoLite2-City.mmdb\n"
            f"    {client_dir}/GeoLite2-ASN.mmdb\n"
        )


def seed_proxies_via_api(
    gateway_port: int,
    api_key: str,
    proxy_specs: list[dict],
    puls_id: str,
    puls_password: str,
) -> int:
    """POST one proxy per port to the running instance's /v1/proxies endpoint.

    Uses stdlib urllib so the provisioner has zero third-party imports. The real
    puls id/password are substituted into the URL only here, in-memory.
    """
    import urllib.error
    import urllib.request

    base = f"http://localhost:{gateway_port}/v1/proxies/"
    seeded = 0
    for spec in proxy_specs:
        url = spec["url"].replace(PULS_ID_PLACEHOLDER, puls_id).replace(
            PULS_PASSWORD_PLACEHOLDER, puls_password
        )
        body = json.dumps(
            {
                "url": url,
                "proxy_type": spec["proxy_type"],
                "country": spec["country"],
                "tz_offset": spec.get("tz_offset", 0),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            base,
            data=body,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status in (200, 201):
                    seeded += 1
                else:
                    print(f"  proxy port {spec['port']}: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            print(f"  proxy port {spec['port']}: HTTP {e.code} {e.reason}")
        except Exception as e:  # noqa: BLE001 - best-effort seeding, keep going
            print(f"  proxy port {spec['port']}: {e}")
    return seeded


def _run_proxy_seeding(
    gateway_port: int,
    api_key: str,
    proxy_specs: list[dict],
    client_id: str,
    n_ports: int,
) -> int:
    """Execute the proxy seeding step (checking for env credentials and calling the API).

    Returns 0 on success (or if credentials missing), 1 if seeding failed.
    Shared by both normal provisioning and --seed-proxies-only runs.
    """
    puls_id = os.environ.get("PULS_PROXY_ID")
    puls_password = os.environ.get("PULS_PROXY_PASSWORD")
    if not puls_id or not puls_password:
        print("WARNING: PULS_PROXY_ID / PULS_PROXY_PASSWORD not in env; skipping live "
              "proxy seed. Set them and re-run to seed proxies.")
        return 0

    print(f"[seed] POSTing {n_ports} proxies to http://localhost:{gateway_port}/v1/proxies ...")
    seeded = seed_proxies_via_api(
        gateway_port, api_key, proxy_specs, puls_id, puls_password
    )
    print(f"[seed] seeded {seeded}/{n_ports} proxies for {client_id}")
    return 0 if seeded == n_ports else 1


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-id", required=True, help="unique client id (folder/db label)")
    p.add_argument("--gateway-port", required=True, type=int, help="host port for the gateway")
    p.add_argument(
        "--proxy-ports",
        required=True,
        help=f"puls port slice START-END within {ENGAGE_PROXY_PORT_MIN}-{ENGAGE_PROXY_PORT_MAX}",
    )
    p.add_argument("--country", "--cc", dest="country", required=True, help="proxy exit country (ISO-2)")
    p.add_argument("--sessttl", type=int, default=30, help="puls session TTL minutes (10-120)")
    p.add_argument("--proxy-type", default="residential",
                   choices=["mobile_4g", "residential", "datacenter"])
    p.add_argument("--tz-offset", type=int, default=0, help="proxy tz offset minutes (Proxy.tz_offset)")
    # Secrets: generated if not supplied. Prefer NOT passing on the CLI.
    p.add_argument("--postgres-password", default=None, help="(generated if omitted)")
    p.add_argument("--api-key", default=None, help="(generated if omitted)")
    p.add_argument("--n8n-webhook-url", default="", help="N8N_SYSTEM_WEBHOOK_URL for this client")
    p.add_argument("--image", default="atomic-engage:latest", help="shared image tag")
    p.add_argument("--client-name", default="", help="human-readable label (registry note)")
    p.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    p.add_argument("--instances-dir", default=str(DEFAULT_INSTANCES_DIR))
    p.add_argument(
        "--geoip-src",
        default=os.environ.get("GEOIP_SRC_DIR", str(FLEET_ROOT / "geoip")),
        help="source directory for GeoLite mmdb files (env: GEOIP_SRC_DIR)",
    )
    p.add_argument("--render-only", action="store_true",
                   help="write files only; no DB, no docker, no network")
    p.add_argument("--seed-proxies", action="store_true",
                   help="render or reuse instance, then run ONLY proxy seeding (requires PULS_PROXY_ID/PASSWORD env)")
    args = p.parse_args()

    if not (10 <= args.sessttl <= 120):
        raise SystemExit(f"--sessttl {args.sessttl} out of range 10-120")
    if len(args.country) != 2 or not args.country.isalpha():
        raise SystemExit(f"--country {args.country!r} must be a 2-letter ISO code")

    # --render-only promises to touch no network; --seed-proxies is a network call.
    if args.render_only and args.seed_proxies:
        raise SystemExit("--render-only and --seed-proxies are mutually exclusive")

    # Validate N8N webhook URL (GAP 1)
    if not args.n8n_webhook_url:
        if args.render_only:
            print("[WARNING] N8N_SYSTEM_WEBHOOK_URL is empty. Instance will not start until webhook URL is configured.")
        elif not args.seed_proxies:
            # Only required for full provisioning (not for --seed-proxies-only)
            raise SystemExit(
                "N8N_SYSTEM_WEBHOOK_URL is required. Pass --n8n-webhook-url <url> "
                "(or set via env fallback if you add one later)"
            )

    proxy_range = parse_port_range(args.proxy_ports)
    template_path = Path(args.template)
    if not template_path.exists():
        raise SystemExit(f"template not found: {template_path}")

    instances_dir = Path(args.instances_dir)
    registry_path = instances_dir / "registry.json"
    registry = load_registry(registry_path)

    # --seed-proxies on its own is the SECOND phase of the documented flow:
    # render-only, bring the stack up, then seed. The client is therefore already
    # in the registry, and re-rendering it would be actively destructive -- it
    # would mint a fresh POSTGRES_PASSWORD and API_KEY over the .env the running
    # containers were started from, so the seed would authenticate with a key the
    # gateway never had and the next `compose up` would hand postgres a password
    # its existing volume does not know. Seed-only therefore reads the live key
    # back out and writes nothing.
    seed_only = args.seed_proxies and not args.render_only

    validate_allocation(
        registry, args.client_id, args.gateway_port, proxy_range,
        allow_existing=seed_only,
    )

    if seed_only:
        client_dir = instances_dir / args.client_id
        env_path = client_dir / ".env"
        if not env_path.exists():
            raise SystemExit(
                f"--seed-proxies expects an already-rendered instance, but "
                f"{env_path} does not exist (run without --seed-proxies first)"
            )
        api_key = read_env_value(env_path, "API_KEY")
        if not api_key:
            raise SystemExit(f"API_KEY not found in {env_path}")
        puls_id = os.environ.get("PULS_PROXY_ID")
        puls_password = os.environ.get("PULS_PROXY_PASSWORD")
        if not puls_id or not puls_password:
            raise SystemExit(
                "PULS_PROXY_ID and PULS_PROXY_PASSWORD must be set in environment "
                "to run --seed-proxies"
            )
        proxy_specs = build_proxy_specs(proxy_range, args)
        n_ports = proxy_range[1] - proxy_range[0] + 1
        print(f"[seed-only] reusing existing instances/{args.client_id}/.env (no re-render)")
        return _run_proxy_seeding(
            args.gateway_port, api_key, proxy_specs, args.client_id, n_ports
        )

    # ── secrets (generated, never logged in full) ───────────────────────────
    postgres_password = args.postgres_password or secrets.token_urlsafe(24)
    api_key = args.api_key or secrets.token_urlsafe(32)
    db_name = f"{slugify_db_ident(args.client_id)}_db"
    db_user = f"{slugify_db_ident(args.client_id)}_user"
    created_at = datetime.now(timezone.utc).isoformat()

    # ── render instance dir ─────────────────────────────────────────────────
    client_dir = instances_dir / args.client_id
    client_dir.mkdir(parents=True, exist_ok=True)

    # Provision GeoLite mmdb files (GAP 2)
    _provision_geoip(client_dir, args.geoip_src)

    # compose: copy the template verbatim; ${VAR} is interpolated by docker compose.
    (client_dir / "docker-compose.yml").write_text(
        template_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    render_env_file(
        client_dir / ".env",
        {
            "CLIENT_ID": args.client_id,
            "GATEWAY_PORT": str(args.gateway_port),
            "POSTGRES_USER": db_user,
            "POSTGRES_PASSWORD": postgres_password,
            "POSTGRES_DB": db_name,
            "API_KEY": api_key,
            "N8N_SYSTEM_WEBHOOK_URL": args.n8n_webhook_url,
            "ENGAGE_IMAGE": args.image,
        },
    )

    # proxies.seed.json — one row per port, password kept as a placeholder.
    proxy_specs = build_proxy_specs(proxy_range, args)
    (client_dir / "proxies.seed.json").write_text(
        json.dumps(proxy_specs, indent=2), encoding="utf-8"
    )

    # ── registry (no secrets) ───────────────────────────────────────────────
    registry.setdefault("instances", {})[args.client_id] = {
        "client_id": args.client_id,
        "client_name": args.client_name,
        "gateway_port": args.gateway_port,
        "proxy_port_range": [proxy_range[0], proxy_range[1]],
        "proxy_country": args.country.upper(),
        "sessttl": args.sessttl,
        "db_name": db_name,
        "db_user": db_user,
        "status": "provisioned",
        "created_at": created_at,
    }
    registry["last_updated"] = created_at
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    n_ports = proxy_range[1] - proxy_range[0] + 1
    print(f"[render] instances/{args.client_id}/  (docker-compose.yml, .env, proxies.seed.json)")
    print(f"[render] db={db_name} user={db_user} gateway_port={args.gateway_port}")
    print(f"[render] proxy slice {proxy_range[0]}-{proxy_range[1]} ({n_ports} ports), country={args.country.upper()}, sessttl={args.sessttl}m")
    print(f"[render] registry -> {registry_path}")

    # Seed-only returned long before here; a fresh provision falls through to the
    # live seed at the end unless --render-only asked for files and nothing else.
    if args.render_only:
        print("[render-only] skipped live proxy seed + docker. Next steps:")
        print(f"  cd instances/{args.client_id} && docker compose --env-file .env up -d")
        print(f"  PULS_PROXY_ID=... PULS_PROXY_PASSWORD=... \\")
        print(f"    python scripts/provision_client.py --seed-proxies --client-id {args.client_id} --gateway-port {args.gateway_port}")
        return 0

    return _run_proxy_seeding(args.gateway_port, api_key, proxy_specs, args.client_id, n_ports)


if __name__ == "__main__":
    sys.exit(main())
