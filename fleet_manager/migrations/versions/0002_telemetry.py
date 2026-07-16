"""telemetry_events table + accounts survival columns (FR-143)

Revision ID: 0002_telemetry
Revises: 0001_initial
Create Date: 2026-06-22

Adds the research instrument: a `telemetry_events` store plus denormalised
`cohort`/`first_seen_at`/`banned_at` on accounts (survival_time = banned_at −
first_seen_at, SC-112).

All DDL is idempotent (IF NOT EXISTS). The 0001 migration builds ordinary tables via
`Base.metadata.create_all(... live metadata ...)`, so on a *fresh* `upgrade head` it
already emits this table and these columns from the current models — the guards make
this migration a no-op there, while still adding them to a DB that ran 0001 before the
model grew.
"""
from alembic import op

revision = "0002_telemetry"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id            BIGSERIAL PRIMARY KEY,
            account_id    BIGINT REFERENCES accounts(id),
            cohort        VARCHAR(64),
            event_type    VARCHAR(40) NOT NULL,
            cause         VARCHAR(200),
            action_type   VARCHAR(30),
            target_kind   VARCHAR(30),
            outcome       VARCHAR(30),
            warmup_params JSONB,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_account "
        "ON telemetry_events (account_id, created_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_cohort "
        "ON telemetry_events (cohort, event_type);"
    )
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS cohort VARCHAR(64);")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS banned_at;")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS first_seen_at;")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS cohort;")
    op.execute("DROP TABLE IF EXISTS telemetry_events;")
