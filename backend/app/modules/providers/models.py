"""Tenant-bound provider records; public application_id remains an external ID.

Every relationship carrying a tenant scope uses a composite foreign key. Link
versions additionally bind their drama to the same connection and application.
Remote effects have a scope identity independent of their configuration digest,
so a different request intent cannot bypass an occupied remote channel.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ProviderConnection(SQLModel, table=True):
    __tablename__ = "provider_connection"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_provider_connection_tenant_id"),
        CheckConstraint(
            "kind IN ('wangyan','jiashu','duiba','gangganhao','rongliang','minglue','other')",
            name="ck_provider_connection_kind",
        ),
        CheckConstraint(
            "status IN ('pending','verifying','active','reauth_required','error','disabled','local')",
            name="ck_provider_connection_status",
        ),
        CheckConstraint(
            "credential_version >= 0", name="ck_provider_connection_version"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    kind: str = Field(max_length=32)
    display_name: str = Field(max_length=255)
    encrypted_credentials: str | None = Field(default=None, repr=False, exclude=True)
    credential_version: int = 0
    status: str = Field(default="pending", max_length=32)
    verification_token: UUID | None = None
    verifying_started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=64)


class ProviderApplication(SQLModel, table=True):
    __tablename__ = "provider_application"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_provider_application_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "external_id",
            name="uq_provider_application_external",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["provider_connection.tenant_id", "provider_connection.id"],
            name="fk_provider_application_connection",
        ),
        Index(
            "ix_provider_application_connection_id", "tenant_id", "connection_id", "id"
        ),
        CheckConstraint(
            "jsonb_typeof(channel_config) = 'object'",
            name="ck_provider_application_config",
        ),
        CheckConstraint(
            "jsonb_typeof(link_defaults) = 'object'",
            name="ck_provider_application_link_defaults",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    connection_id: UUID
    external_id: str = Field(max_length=255)
    name: str = Field(max_length=255)
    channel_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # 管理员维护的取链默认值；与版权方发现写入的 channel_config 分开，重新发现不覆盖。
    link_defaults: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    tiktok_minis_id: str | None = Field(default=None, max_length=255)


class ProviderDrama(SQLModel, table=True):
    __tablename__ = "provider_drama"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_provider_drama_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "application_id",
            "id",
            name="uq_provider_drama_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "application_id",
            "external_drama_id",
            name="uq_provider_drama_external",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_provider_drama_application",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    connection_id: UUID
    application_id: str = Field(max_length=255)
    external_drama_id: str = Field(max_length=255)
    # 对外展示的版权方编号；接口取链仍使用 external_drama_id。
    display_drama_id: str | None = Field(default=None, max_length=255)
    title: str = Field(max_length=1024)
    language: str | None = Field(default=None, max_length=64)


class PromotionLink(SQLModel, table=True):
    __tablename__ = "promotion_link"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_promotion_link_tenant_id"),
        UniqueConstraint(
            "tenant_id", "reuse_key", "version", name="uq_promotion_link_version"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id", "drama_id"],
            [
                "provider_drama.tenant_id",
                "provider_drama.connection_id",
                "provider_drama.application_id",
                "provider_drama.id",
            ],
            name="fk_promotion_link_drama_identity",
        ),
        Index(
            "uq_promotion_link_current_ready",
            "tenant_id",
            "reuse_key",
            unique=True,
            postgresql_where=text("status = 'ready'"),
        ),
        Index(
            "ix_promotion_link_drama",
            "tenant_id",
            "connection_id",
            "application_id",
            "drama_id",
        ),
        CheckConstraint("version > 0", name="ck_promotion_link_version"),
        CheckConstraint(
            "source IN ('provider','manual')", name="ck_promotion_link_source"
        ),
        CheckConstraint(
            "status IN ('pending','ready','superseded','invalid','result_unknown','failed')",
            name="ck_promotion_link_status",
        ),
        CheckConstraint(
            "jsonb_typeof(config) = 'object' AND jsonb_typeof(attribution) = 'object'",
            name="ck_promotion_link_json",
        ),
        CheckConstraint(
            "status != 'ready' OR (url IS NOT NULL AND length(btrim(url)) > 0 AND protected_base IS NOT NULL AND verified_at IS NOT NULL)",
            name="ck_promotion_link_ready",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    source: str = Field(default="provider", max_length=16)
    reuse_key: str = Field(max_length=64)
    drama_id: UUID
    connection_id: UUID
    application_id: str = Field(max_length=255)
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    remote_id: str | None = Field(default=None, max_length=255)
    url: str | None = None
    protected_base: str | None = None
    attribution: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    version: int = 1
    verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    status: str = Field(default="pending", max_length=32)


class LinkPreparation(SQLModel, table=True):
    __tablename__ = "link_preparation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_link_preparation_tenant_id"),
        UniqueConstraint("tenant_id", "request_id", name="uq_link_preparation_request"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_link_preparation_application",
        ),
        Index("ix_link_preparation_connection_id", "tenant_id", "connection_id", "id"),
        CheckConstraint(
            "status IN ('pending','running','ready','partial_ready','failed')",
            name="ck_link_preparation_status",
        ),
        CheckConstraint(
            "jsonb_typeof(config) = 'object'", name="ck_link_preparation_config"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    actor_id: UUID = Field(foreign_key="user.id")
    request_id: UUID
    request_digest: str = Field(max_length=64)
    connection_id: UUID
    application_id: str = Field(max_length=255)
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    status: str = Field(default="pending", max_length=32)


class LinkPreparationItem(SQLModel, table=True):
    __tablename__ = "link_preparation_item"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_link_preparation_item_tenant_id"),
        UniqueConstraint(
            "preparation_id", "line_no", name="uq_link_preparation_item_line"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["link_preparation.tenant_id", "link_preparation.id"],
            name="fk_link_preparation_item_preparation",
        ),
        Index(
            "ix_link_preparation_item_page",
            "tenant_id",
            "preparation_id",
            "line_no",
            "id",
        ),
        CheckConstraint("line_no > 0", name="ck_link_preparation_item_line"),
        CheckConstraint(
            "status IN ('pending','resolving','checking','creating','verifying','needs_resolution','blocked_auth','config_conflict','retryable_error','result_unknown','failed','ready')",
            name="ck_link_preparation_item_status",
        ),
        CheckConstraint(
            "jsonb_typeof(resolved) = 'object'",
            name="ck_link_preparation_item_resolved",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    preparation_id: UUID
    line_no: int
    raw_input: str
    resolved: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    status: str = Field(default="pending", max_length=32)


class ProviderRemoteScope(SQLModel, table=True):
    __tablename__ = "provider_remote_scope"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_provider_remote_scope_tenant_id"),
        UniqueConstraint("tenant_id", "scope_key", name="uq_provider_remote_scope_key"),
        ForeignKeyConstraint(
            ["tenant_id", "active_item_id"],
            ["link_preparation_item.tenant_id", "link_preparation_item.id"],
            name="fk_provider_remote_scope_active_item",
        ),
        Index("ix_provider_remote_scope_active_item", "tenant_id", "active_item_id"),
        CheckConstraint(
            "status IN ('idle','held','result_unknown')",
            name="ck_provider_remote_scope_status",
        ),
        CheckConstraint(
            "(status = 'idle' AND active_item_id IS NULL) OR (status IN ('held','result_unknown') AND active_item_id IS NOT NULL)",
            name="ck_provider_remote_scope_owner",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    scope_key: str = Field(max_length=255)
    active_item_id: UUID | None = None
    status: str = Field(default="idle", max_length=32)


class ProviderEffect(SQLModel, table=True):
    __tablename__ = "provider_effect"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_provider_effect_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "remote_scope_key",
            "step",
            "request_digest",
            name="uq_provider_effect_intent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "remote_scope_key"],
            ["provider_remote_scope.tenant_id", "provider_remote_scope.scope_key"],
            name="fk_provider_effect_remote_scope",
        ),
        CheckConstraint(
            "status IN ('pending','confirmed_absent','sending','result_unknown','succeeded','failed')",
            name="ck_provider_effect_status",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object'", name="ck_provider_effect_result"
        ),
        CheckConstraint(
            "status != 'sending' OR attempt_token IS NOT NULL",
            name="ck_provider_effect_sending_token",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    remote_scope_key: str = Field(max_length=255)
    step: str = Field(max_length=64)
    request_digest: str = Field(max_length=64)
    status: str = Field(default="pending", max_length=32)
    attempt_token: UUID | None = None
    remote_id: str | None = Field(default=None, max_length=255)
    result: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )


class ProviderSessionRefresh(SQLModel, table=True):
    """One fenced refresh per connection; candidate session remains encrypted."""

    __tablename__ = "provider_session_refresh"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["provider_connection.tenant_id", "provider_connection.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "phase IN ('login','applications','options','complete','failed')",
            name="ck_provider_refresh_phase",
        ),
        CheckConstraint(
            "attempts >= 0 AND login_attempts >= 0 AND option_index >= 0 AND cooldown_rounds >= 0",
            name="ck_provider_refresh_counters",
        ),
    )
    connection_id: UUID = Field(primary_key=True)
    tenant_id: UUID = Field(index=True)
    credential_version: int
    verification_token: UUID
    phase: str = "login"
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    due_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    window_started_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    cooldown_rounds: int = 0
    unavailable_since: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    attempts: int = 0
    login_attempts: int = 0
    option_index: int = 0
    candidate_ciphertext: str | None = Field(default=None, repr=False, exclude=True)
    applications: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
