from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="warmup")
    warmup_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fresh"
    )
    use_case: Mapped[str] = mapped_column(String(20), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_country: Mapped[str] = mapped_column(String(2), nullable=False)
    session_string: Mapped[str] = mapped_column(Text, nullable=False)
    api_credential_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_credentials.id"), nullable=False
    )
    proxy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("proxies.id"), nullable=False
    )
    device_model: Mapped[str] = mapped_column(String(100), nullable=False)
    system_version: Mapped[str] = mapped_column(String(20), nullable=False)
    app_version: Mapped[str] = mapped_column(String(20), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(10), nullable=False)
    system_lang_code: Mapped[str] = mapped_column(String(10), nullable=False)
    work_start: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    work_end: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    flood_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ban_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    warmup_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Research-instrument denormalisation (FR-143): cohort label + survival window.
    # survival_time = banned_at - first_seen_at, compared across cohorts (SC-112).
    cohort: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    banned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=func.now()
    )

    api_credential: Mapped["ApiCredential"] = relationship(
        "ApiCredential", back_populates="accounts"
    )
    proxy: Mapped["Proxy"] = relationship("Proxy", back_populates="accounts")
    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="account")


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    api_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    accounts: Mapped[list["Account"]] = relationship(
        "Account", back_populates="api_credential"
    )


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    proxy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    asn: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tz_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="reserve")
    is_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="proxy")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    deferred_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=func.now()
    )

    account: Mapped["Account"] = relationship("Account", back_populates="tasks")
    __table_args__ = (
        Index("ix_tasks_account_status", "account_id", "status"),
        Index("ix_tasks_status_priority", "status", "priority"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )


class GlobalPeer(Base):
    __tablename__ = "global_peers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # FR-114: peer_id is globally unique (username -> peer_id mapping).
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=func.now()
    )


class PeerAccessHash(Base):
    """Per-account access hashes (Principle IV / FR-109).

    Composite PK (account_id, peer_id); the physical table is RANGE-partitioned by
    account_id (10 partitions) — partitioning is created by the Alembic migration in
    raw SQL (SQLAlchemy cannot declare PARTITION BY). access_hash is the 64-bit
    Telegram value and is NEVER shared across accounts or stored in Redis.
    """

    __tablename__ = "peer_access_hashes"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), primary_key=True
    )
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    access_hash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_min: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    obtained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )


class WarmupCrossPair(Base):
    __tablename__ = "warmup_cross_pairs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    target_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    source_account: Mapped["Account"] = relationship(
        "Account", foreign_keys=[source_account_id]
    )
    target_account: Mapped["Account"] = relationship(
        "Account", foreign_keys=[target_account_id]
    )


class TelemetryEvent(Base):
    """Behaviour & ban telemetry — the experiment's primary output (FR-143).

    Records only the behavioural SHAPE of fleet events: lifecycle transitions with
    cause+timestamp, the warmup/limit params in force, and a per-action log
    (type, target-kind, outcome). Stores NO message content and NO PII, so two cohorts
    run under different safety parameters can be exported and compared (SC-112).
    `account_id` is nullable so an event survives the account row it describes.
    """
    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=True
    )
    cohort: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # onboarded|warmup_tier|flood|sleeping|banned|action|survival_tick
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    cause: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    action_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    target_kind: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    warmup_params: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    __table_args__ = (
        Index("idx_telemetry_account", "account_id", "created_at"),
        Index("idx_telemetry_cohort", "cohort", "event_type"),
    )
