"""accounts.geo_override — acknowledged phone/proxy country divergence

Revision ID: 0005_account_geo_override
Revises: 0004_unfreeze_timestamp_defaults
Create Date: 2026-08-08

The geo gate compares an account's phone country with its proxy exit country and, on
any mismatch, puts the account to sleep. That is the right default: a number and an IP
from different countries is a signal Telegram can read.

It stops being the right default for a fleet bought as one batch, where the seller's
numbers come from whatever countries were available (+1, +33, +44, +992, +998 in the
case that prompted this) while the operator deliberately runs all of them behind a
single stable ISP exit. There, exit *stability* is what protects the accounts and the
country divergence is a deliberate trade.

The two ways to express that without this column were both bad: relax the gate globally
and lose it for every future account, or write a false country onto the proxy row and
corrupt the data every other component reads. This flag scopes the exemption to the one
account it was granted for, and the worker logs it on every use so it stays visible
instead of becoming a silent permanent hole.

Defaults to false, so existing accounts keep the strict behaviour.
"""
from alembic import op

revision = "0005_account_geo_override"
down_revision = "0004_unfreeze_timestamp_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS geo_override "
        "BOOLEAN NOT NULL DEFAULT false;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS geo_override;")
