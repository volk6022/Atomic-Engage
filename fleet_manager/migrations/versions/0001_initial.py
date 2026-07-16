"""initial schema: all entities + partitioned peer_access_hashes

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-04

Creates every entity. All ordinary tables are emitted from the ORM metadata so they
stay in lock-step with app.db.models. `peer_access_hashes` is created by hand as a
RANGE-partitioned table (by account_id, 10 partitions of 1000 accounts) because
SQLAlchemy cannot declare PARTITION BY — this is the structural enforcement of
Principle IV / FR-109.
"""
from alembic import op

from app.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_PARTITIONED = "peer_access_hashes"
_N_PARTITIONS = 10
_PARTITION_SPAN = 1000  # accounts per partition


def _ordinary_tables():
    """All mapped tables except the partitioned one, in FK-dependency order."""
    return [t for t in Base.metadata.sorted_tables if t.name != _PARTITIONED]


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Ordinary tables straight from the ORM metadata (accounts, api_credentials,
    #    proxies, tasks, webhook_deliveries, global_peers, warmup_cross_pairs) with
    #    all their indexes/constraints — guaranteed to match the models.
    Base.metadata.create_all(bind=bind, tables=_ordinary_tables())

    # 2. Partitioned peer_access_hashes: composite PK (account_id, peer_id),
    #    access_hash BIGINT, RANGE-partitioned by account_id.
    op.execute(
        """
        CREATE TABLE peer_access_hashes (
            account_id  BIGINT      NOT NULL REFERENCES accounts(id),
            peer_id     BIGINT      NOT NULL,
            access_hash BIGINT      NOT NULL,
            is_min      BOOLEAN     NOT NULL DEFAULT FALSE,
            obtained_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id, peer_id)
        ) PARTITION BY RANGE (account_id);
        """
    )
    for i in range(_N_PARTITIONS):
        low = i * _PARTITION_SPAN
        high = (i + 1) * _PARTITION_SPAN
        op.execute(
            f"CREATE TABLE pah_{i}k PARTITION OF peer_access_hashes "
            f"FOR VALUES FROM ({low}) TO ({high});"
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Dropping the parent cascades to all pah_* partitions.
    op.execute("DROP TABLE IF EXISTS peer_access_hashes CASCADE;")
    Base.metadata.drop_all(bind=bind, tables=_ordinary_tables())
