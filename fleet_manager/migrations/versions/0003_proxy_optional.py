"""accounts.proxy_id nullable — support running an account on the host IP (no proxy)

Revision ID: 0003_proxy_optional
Revises: 0002_telemetry
Create Date: 2026-07-03

A NULL proxy_id means "use the host machine's own IP" instead of a pool proxy. This is
an intentional mode for a residential-IP box whose reputation beats the proxy segment.
The worker prepare/execute path already handles account.proxy is None (geo gate skips
the comparison, the datacenter-ASN gate is guarded on `proxy is not None`, and proxy
rotation falls back to a plain failure), so only the schema constraint needed relaxing.

Idempotent: DROP NOT NULL is safe to re-run.
"""
from alembic import op

revision = "0003_proxy_optional"
down_revision = "0002_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ALTER COLUMN proxy_id DROP NOT NULL;")


def downgrade() -> None:
    # Only re-add NOT NULL if no proxy-less rows exist; otherwise leave it relaxed.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM accounts WHERE proxy_id IS NULL) THEN
                ALTER TABLE accounts ALTER COLUMN proxy_id SET NOT NULL;
            END IF;
        END $$;
        """
    )
