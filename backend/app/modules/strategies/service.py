"""Short, tenant-scoped transactions; callers commit, no external operations."""

import hashlib
import json
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.strategies.copy_pool import CopyChoice
from app.modules.strategies.models import (
    CopyEntry,
    CopyPoolVersion,
    Strategy,
    StrategyVersion,
)
from app.modules.strategies.naming import validate_name_template
from app.modules.strategies.saved_config import read_saved_config
from app.modules.strategies.schemas import (
    CopyPoolPublic,
    CopyPublic,
    StrategyConfig,
    StrategyPublic,
    ValidationIssue,
    VersionPublic,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant


def _authorize(
    session: Session, context: TenantContext, *, write: bool = False
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def get_copy_pool(
    session: Session, *, context: TenantContext, version_id: UUID
) -> CopyPoolPublic:
    _authorize(session, context)
    pool = session.get(CopyPoolVersion, version_id, populate_existing=True)
    if pool is None or not pool.sealed:
        raise DomainError("copy_pool_not_found", "copy_pool_not_found")
    entries = session.exec(
        select(CopyEntry)
        .where(
            CopyEntry.pool_version_id == version_id, col(CopyEntry.enabled).is_(True)
        )
        .order_by(col(CopyEntry.position))
    ).all()
    return CopyPoolPublic(
        id=pool.id,
        name=pool.name,
        entries=[
            CopyPublic(id=entry.id, text=entry.text, position=entry.position)
            for entry in entries
        ],
    )


def get_copies(
    session: Session, *, context: TenantContext, version_id: UUID
) -> tuple[CopyChoice, ...]:
    return tuple(
        CopyChoice(item.id, item.text)
        for item in get_copy_pool(
            session, context=context, version_id=version_id
        ).entries
    )


def validate_strategy(
    session: Session, *, context: TenantContext, config: StrategyConfig
) -> list[ValidationIssue]:
    _authorize(session, context)
    try:
        config = StrategyConfig.model_validate(config.model_dump())
    except ValidationError as error:
        # API callers receive one field-level issue per contract violation.  No
        # material inventory is consulted when the structure itself is invalid.
        issues: list[ValidationIssue] = []
        for item in error.errors():
            message = str(item.get("msg", "configuration_invalid"))
            location = item.get("loc") or ()
            located_field = location[-1] if location else None
            field = next(
                (
                    candidate
                    for candidate in (
                        "budget",
                        "currency",
                        "group_generation_mode",
                        "group_count",
                        "group_material_allocation",
                        "max_materials_per_group",
                        "ad_generation_mode",
                        "ads_per_group",
                        "ad_material_allocation",
                        "max_materials_per_ad",
                        "creative_count",
                        "target_roas",
                        "copy_pool_version",
                        "cta_option_ids",
                        "campaign_name_template",
                    )
                    if candidate == located_field or candidate in message
                ),
                "config",
            )
            # Pydantic 的模型级错误 loc 为空，只能从业务消息恢复字段；无法
            # 恢复时保留 config，调用方仍可按稳定错误码展示整段配置错误。
            if field == "config" and located_field in {
                "budget",
                "currency",
                "group_generation_mode",
                "group_count",
                "group_material_allocation",
                "max_materials_per_group",
                "ad_generation_mode",
                "ads_per_group",
                "ad_material_allocation",
                "max_materials_per_ad",
                "creative_count",
                "target_roas",
                "copy_pool_version",
                "cta_option_ids",
                "campaign_name_template",
            }:
                field = located_field
            code = (
                "target_roas_required"
                if "required for TARGET_ROAS" in message
                else "target_roas_forbidden"
                if "only valid for TARGET_ROAS" in message
                else "configuration_invalid"
            )
            issues.append(ValidationIssue(field=field, code=code))
        return issues
    errors = []
    try:
        config.targeting.automatic_minimum_age()
    except DomainError as error:
        errors.append(ValidationIssue(field="targeting", code=error.code))
    try:
        copies = get_copies(
            session, context=context, version_id=config.copy_pool_version
        )
        if config.creative_count > len(
            {copy.text for copy in copies if copy.text.strip()}
        ):
            errors.append(
                ValidationIssue(field="creative_count", code="copy_pool_exhausted")
            )
    except DomainError as error:
        if error.code != "copy_pool_not_found":
            raise
        errors.append(ValidationIssue(field="copy_pool_version", code=error.code))
    try:
        validate_name_template(config.campaign_name_template)
    except DomainError as error:
        errors.append(ValidationIssue(field="campaign_name_template", code=error.code))
    if len(set(config.cta_option_ids)) != len(config.cta_option_ids) or any(
        not option.strip() or len(option) > 255 for option in config.cta_option_ids
    ):
        errors.append(
            ValidationIssue(field="cta_option_ids", code="invalid_cta_options")
        )
    # Official scene/CTA capabilities are checked per target during preview.
    return errors


def _checked_config(
    session: Session, context: TenantContext, config: StrategyConfig
) -> StrategyConfig:
    validated = StrategyConfig.model_validate(config.model_dump())
    issues = validate_strategy(session, context=context, config=validated)
    if issues:
        raise DomainError(issues[0].code, issues[0].code)
    return validated


def _digest(intent: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _request(
    session: Session, context: TenantContext, request_id: UUID, digest: str
) -> StrategyVersion | None:
    # Serializes only the same tenant/request before a create has a strategy row.
    key = int.from_bytes(
        hashlib.sha256(f"strategy:{context.tenant_id}:{request_id}".encode()).digest()[
            :8
        ],
        signed=True,
    )
    cast(SQLAlchemySession, session).execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
    )
    _authorize(session, context, write=True)
    existing = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.tenant_id == context.tenant_id,
            StrategyVersion.request_id == request_id,
        )
    ).one_or_none()
    if existing and existing.request_digest != digest:
        raise DomainError("idempotency_conflict", "idempotency_conflict")
    return existing


def _strategy(
    session: Session, context: TenantContext, strategy_id: UUID, *, lock: bool = False
) -> Strategy:
    query = select(Strategy).where(
        Strategy.tenant_id == context.tenant_id, Strategy.id == strategy_id
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("strategy_not_found", "strategy_not_found")
    return row


def _strategy_name(name: str) -> str:
    normalized = name.strip()
    if not normalized or len(normalized) > 120:
        raise DomainError("invalid_strategy_name", "invalid_strategy_name")
    return normalized


def _rename_strategy(
    session: Session, context: TenantContext, strategy: Strategy, name: str
) -> None:
    # 名称只是当前显示标签；配置版本与已提交任务继续引用原有不可变版本。
    if strategy.name == name:
        return
    old_name = strategy.name
    strategy.name = name
    session.add(strategy)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="strategy.rename",
            target_id=str(strategy.id),
            details={"old_name": old_name, "name": name},
        )
    )


def _insert_version(
    session: Session,
    context: TenantContext,
    strategy: Strategy,
    config: StrategyConfig,
    request_id: UUID,
    digest: str,
    kind: str,
) -> UUID:
    strategy.latest_version += 1
    row = StrategyVersion(
        tenant_id=context.tenant_id,
        strategy_id=strategy.id,
        number=strategy.latest_version,
        copy_pool_version_id=config.copy_pool_version,
        config=config.model_dump(mode="json"),
        budget=config.budget,
        target_roas=config.target_roas,
        created_by=context.actor_id,
        request_id=request_id,
        request_digest=digest,
        request_kind=kind,
    )
    session.add(strategy)
    session.add(row)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action=f"strategy.{kind}",
            target_id=str(strategy.id),
            details={
                "version_id": str(row.id),
                "number": row.number,
                "request_id": str(request_id),
            },
        )
    )
    session.flush()
    return row.id


def create_strategy(
    session: Session,
    *,
    context: TenantContext,
    name: str,
    config: StrategyConfig,
    request_id: UUID | None = None,
) -> UUID:
    _authorize(session, context, write=True)
    name = _strategy_name(name)
    config = _checked_config(session, context, config)
    request_id = request_id or uuid4()
    digest = _digest(
        {"kind": "create", "name": name, "config": config.model_dump(mode="json")}
    )
    previous = _request(session, context, request_id, digest)
    if previous:
        return previous.strategy_id
    row = Strategy(tenant_id=context.tenant_id, name=name)
    session.add(row)
    session.flush()
    _insert_version(session, context, row, config, request_id, digest, "create")
    return row.id


def append_version(
    session: Session,
    *,
    context: TenantContext,
    strategy_id: UUID,
    config: StrategyConfig,
    name: str | None = None,
    expected_version: int | None = None,
    request_id: UUID | None = None,
) -> UUID:
    _authorize(session, context, write=True)
    config = _checked_config(session, context, config)
    if name is not None:
        name = _strategy_name(name)
    request_id = request_id or uuid4()
    digest = _digest(
        {
            "kind": "append",
            "strategy_id": str(strategy_id),
            "name": name,
            "config": config.model_dump(mode="json"),
            "expected_version": expected_version,
        }
    )
    previous = _request(session, context, request_id, digest)
    if previous:
        return previous.id
    row = _strategy(session, context, strategy_id, lock=True)
    _authorize(session, context, write=True)
    if not row.active:
        raise DomainError("strategy_not_found", "strategy_not_found")
    if expected_version is not None and row.latest_version != expected_version:
        raise DomainError("version_conflict", "version_conflict")
    if name is not None:
        _rename_strategy(session, context, row, name)
    return _insert_version(session, context, row, config, request_id, digest, "append")


def get_version_record(
    session: Session, *, context: TenantContext, version_id: UUID | None
) -> VersionPublic:
    _authorize(session, context)
    row = session.exec(
        select(StrategyVersion)
        .where(
            StrategyVersion.tenant_id == context.tenant_id,
            StrategyVersion.id == version_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("strategy_not_found", "strategy_not_found")
    return VersionPublic(
        id=row.id,
        strategy_id=row.strategy_id,
        number=row.number,
        config=read_saved_config(row.config),
        created_by=row.created_by,
        created_at=row.created_at,
        request_id=row.request_id,
    )


def get_version(
    session: Session, *, context: TenantContext, version_id: UUID
) -> StrategyConfig:
    return get_version_record(session, context=context, version_id=version_id).config


def get_strategy(
    session: Session, *, context: TenantContext, strategy_id: UUID
) -> StrategyPublic:
    _authorize(session, context)
    row = _strategy(session, context, strategy_id)
    version = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.tenant_id == context.tenant_id,
            StrategyVersion.strategy_id == row.id,
            StrategyVersion.number == row.latest_version,
        )
    ).one()
    return _public(row, version)


def _public(row: Strategy, version: StrategyVersion) -> StrategyPublic:
    return StrategyPublic(
        id=row.id,
        name=row.name,
        active=row.active,
        latest_version=row.latest_version,
        version_id=version.id,
        config=read_saved_config(version.config),
        created_by=version.created_by,
        created_at=version.created_at,
    )


def update_strategy(
    session: Session,
    *,
    context: TenantContext,
    strategy_id: UUID,
    active: bool | None = None,
    name: str | None = None,
) -> StrategyPublic:
    _authorize(session, context, write=True)
    row = _strategy(session, context, strategy_id, lock=True)
    _authorize(session, context, write=True)
    if active is None and name is None:
        raise DomainError("configuration_invalid", "configuration_invalid")
    if active is not None and type(active) is not bool:
        raise DomainError("configuration_invalid", "configuration_invalid")
    if name is not None:
        _rename_strategy(session, context, row, _strategy_name(name))
    if active is not None and row.active != active:
        row.active = active
        session.add(row)
        session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                action="strategy.set_active",
                target_id=str(row.id),
                details={"active": active},
            )
        )
    session.flush()
    return get_strategy(session, context=context, strategy_id=row.id)


def set_active(
    session: Session, *, context: TenantContext, strategy_id: UUID, active: bool
) -> StrategyPublic:
    return update_strategy(
        session,
        context=context,
        strategy_id=strategy_id,
        active=active,
    )


def list_strategies(
    session: Session,
    *,
    context: TenantContext,
    query: str = "",
    active: bool | None = True,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[StrategyPublic]:
    _authorize(session, context)
    if type(limit) is not int or not 1 <= limit <= 200 or len(query) > 255:
        raise DomainError("configuration_invalid", "configuration_invalid")
    scope = {
        "kind": "strategies",
        "tenant_id": str(context.tenant_id),
        "query": query,
        "active": None if active is None else str(active),
    }
    after = decode_cursor(cursor, scope=scope)
    statement = (
        select(Strategy, StrategyVersion)
        .join(
            StrategyVersion,
            and_(
                col(StrategyVersion.tenant_id) == Strategy.tenant_id,
                col(StrategyVersion.strategy_id) == Strategy.id,
                col(StrategyVersion.number) == Strategy.latest_version,
            ),
        )
        .where(Strategy.tenant_id == context.tenant_id)
    )
    if query.strip():
        statement = statement.where(
            col(Strategy.name).icontains(query.strip(), autoescape=True)
        )
    if active is not None:
        statement = statement.where(Strategy.active == active)
    total = count_rows(session, statement)
    if after:
        statement = statement.where(Strategy.id > UUID(after))
    rows = session.exec(
        statement.order_by(col(Strategy.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    return Page(
        items=[_public(row, version) for row, version in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1][0].id))
        if len(rows) > limit
        else None,
        total=total,
    )


def list_versions(
    session: Session,
    *,
    context: TenantContext,
    strategy_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[VersionPublic]:
    _authorize(session, context)
    _strategy(session, context, strategy_id)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise DomainError("configuration_invalid", "configuration_invalid")
    scope = {
        "kind": "strategy_versions",
        "tenant_id": str(context.tenant_id),
        "strategy_id": str(strategy_id),
    }
    after = decode_cursor(cursor, scope=scope)
    statement = select(StrategyVersion).where(
        StrategyVersion.tenant_id == context.tenant_id,
        StrategyVersion.strategy_id == strategy_id,
    )
    total = count_rows(session, statement)
    if after:
        try:
            statement = statement.where(StrategyVersion.number < int(after))
        except ValueError:
            raise DomainError("invalid_cursor", "invalid_cursor") from None
    rows = session.exec(
        statement.order_by(col(StrategyVersion.number).desc()).limit(limit + 1)
    ).all()
    return Page(
        items=[
            VersionPublic(
                id=row.id,
                strategy_id=row.strategy_id,
                number=row.number,
                config=read_saved_config(row.config),
                created_by=row.created_by,
                created_at=row.created_at,
                request_id=row.request_id,
            )
            for row in rows[:limit]
        ],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].number))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_saved_request(
    session: Session, *, context: TenantContext, request_id: UUID
) -> VersionPublic:
    _authorize(session, context)
    row = session.exec(
        select(StrategyVersion.id).where(
            StrategyVersion.tenant_id == context.tenant_id,
            StrategyVersion.request_id == request_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("strategy_not_found", "strategy_not_found")
    return get_version_record(session, context=context, version_id=row)
