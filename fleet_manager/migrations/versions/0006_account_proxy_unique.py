"""accounts.proxy_id — UNIQUE: one proxy belongs to exactly one account

Revision ID: 0006_account_proxy_unique
Revises: 0005_account_geo_override
Create Date: 2026-09-06

The 1:1 binding between an account and its proxy is stated everywhere the fleet's
safety is reasoned about: two accounts behind one exit are two accounts Telegram can
link by a single signal. The schema never enforced it, so the only way to discover a
violation was by its consequences — and the 18.08 outage was about proxies.

NULL stays shared on purpose. `proxy_id IS NULL` means "run on the host's own IP", a
deliberate mode for a box whose residential address beats the proxy pool, and any
number of accounts may sit there. Postgres treats NULLs as distinct, so a plain UNIQUE
expresses exactly that rule — no partial index needed.

Checked before shipping: none of the live databases (vertsanov, clienta, clientb) has a
duplicate, so the index builds without a data fix. `IF NOT EXISTS` keeps the migration
idempotent on an environment where it was applied by hand.
"""
from alembic import op

revision = "0006_account_proxy_unique"
down_revision = "0005_account_geo_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_proxy_id "
        "ON accounts (proxy_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_accounts_proxy_id;")
