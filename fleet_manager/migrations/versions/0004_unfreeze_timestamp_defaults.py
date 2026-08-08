"""Unfreeze every created_at/updated_at default — they were fixed at DDL time

Revision ID: 0004_unfreeze_timestamp_defaults
Revises: 0003_proxy_optional
Create Date: 2026-08-08

The models declared ``server_default="now()"`` — a plain Python string, which SQLAlchemy
renders as a quoted literal. Postgres evaluated it once, when the table was created, and
stored the *result*:

    tasks.created_at = '2026-07-20 21:05:36.920822+00'::timestamp with time zone

So every row in every table carried the instant the schema was built, forever. Confirmed
on a live instance and reproduced on a temp table: two inserts a second apart came back
with an identical stamp.

What it broke: the task FIFO orders by ``(priority, created_at, id)`` and silently
degenerated to id order; ``updated_at`` was never set on insert; and every time-based
reading of this data — account survival windows (FR-143), task age, delivery latency —
was fiction. Anything built on top that shows a timeline needs this fixed first.

``func.now()`` in the models renders unquoted, so new databases are correct. This
migration repairs existing ones.

Existing values are deliberately NOT rewritten. The real timestamps were never recorded
and cannot be recovered; back-filling them with `now()` would replace a visibly wrong
value with an invisibly wrong one. Rows created before this migration keep the frozen
stamp and should be read as "unknown, predates 0004".

Idempotent: SET DEFAULT is safe to re-run.
"""
from alembic import op

revision = "0004_unfreeze_timestamp_defaults"
down_revision = "0003_proxy_optional"
branch_labels = None
depends_on = None

# table -> timestamp columns carrying a creation/update default
_COLUMNS = {
    "accounts": ("created_at", "updated_at"),
    "api_credentials": ("created_at",),
    "proxies": ("created_at",),
    "tasks": ("created_at", "updated_at"),
    "webhook_deliveries": ("created_at",),
    "global_peers": ("created_at", "updated_at"),
    "peer_access_hashes": ("obtained_at",),
    "warmup_cross_pairs": ("created_at",),
    "telemetry_events": ("created_at",),
}


def upgrade() -> None:
    for table, columns in _COLUMNS.items():
        for column in columns:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT now();"
            )


def downgrade() -> None:
    # Restoring the defect would mean re-freezing the default to this moment, which is
    # strictly worse than leaving a working now(). Downgrade drops the default instead:
    # the column keeps its type and data, and inserts must then supply a value.
    for table, columns in _COLUMNS.items():
        for column in columns:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT;")
