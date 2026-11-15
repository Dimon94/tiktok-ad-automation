"""One durable provider work unit, at most one external request per invocation.

Task 4 must run units in Celery prefork with a 45s hard limit, shorter than the
60s database claim, and schedule expired claims durably. A sending/unknown write
is recovered by read-back, never reset to pending merely because a worker died.
"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, update
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.providers.adapters.contract import LinkReceipt, ProviderClient
from app.modules.providers.adapters.duiba import DuibaClient
from app.modules.providers.adapters.gangganhao import (
    GangganhaoClient,
    episode_range_valid,
)
from app.modules.providers.adapters.rongliang import RongliangClient
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
    ProviderRemoteScope,
)
from app.modules.providers.repository import (
    claim_remote_scope,
    find_ready_link,
    get_or_create_effect,
)
from app.modules.providers.schemas import DramaCandidate, ResolvedLink, link_reuse_key
from app.modules.tenants.models import Tenant
from app.modules.tenants.permissions import require_tenant

CLAIM_SECONDS = 60
TERMINAL = {"ready", "needs_resolution", "failed", "config_conflict", "blocked_auth"}
STOPPED_STAGES = frozenset({"invalid", "manual_review"})


def runnable_link_item(item: LinkPreparationItem) -> bool:
    """未知写入仍按原任务核查；损坏或已确认歧义的任务交人工处理。"""
    return (
        item.status not in TERMINAL
        and item.resolved.get("_work", {}).get("stage") not in STOPPED_STAGES
    )


def runnable_link_condition():
    return and_(
        col(LinkPreparationItem.status).not_in(TERMINAL),
        *(
            col(LinkPreparationItem.resolved)["_work"]["stage"].astext.is_distinct_from(
                stage
            )
            for stage in sorted(STOPPED_STAGES)
        ),
    )


def has_runnable_link_items(
    session: Session,
    *,
    tenant_id: UUID,
    preparation_id: UUID,
    item_ids: list[UUID] | None = None,
) -> bool:
    """同一可继续合同的有界查询；不向草稿层复制一份状态集合。"""
    query = select(LinkPreparationItem.id).where(
        LinkPreparationItem.tenant_id == tenant_id,
        LinkPreparationItem.preparation_id == preparation_id,
        runnable_link_condition(),
    )
    if item_ids is not None:
        query = query.where(col(LinkPreparationItem.id).in_(item_ids))
    return session.exec(query.limit(1)).first() is not None


ERROR_MESSAGES = {
    "config_conflict": "已有渠道配置与本次请求不一致，未覆盖远端配置",
    "config_unverifiable": "已有渠道配置无法完整核实",
    "lookup_incomplete": "尚不能证明历史链接查询完整，未创建新链接",
    "attribution_contract_unverified": "版权方归因契约尚未核实",
    "provider_result_unknown": "远端结果待核实，不会自动重放未知写入",
    "provider_unavailable": "版权方暂不可用，将从当前步骤重试",
    "provider_scope_busy": "该远端渠道正在处理另一项请求",
    "provider_session_expired": "版权方会话已过期，正在自动恢复",
    "provider_session_refreshing": "正在自动恢复版权方连接，随后继续当前步骤",
    "provider_captcha_failed": "验证码自动识别未成功，请重新验证版权方连接",
    "provider_auth_failed": "版权方账号或密码不可用，请管理员更新连接凭据",
    "provider_session_refresh_failed": "版权方连接恢复暂未成功，请检查连接状态",
    "connection_unavailable": "版权方连接不可用，请检查连接状态",
    "provider_application_forbidden": "该连接不能访问当前应用",
    "provider_credentials_changed": "连接认证已变化，请重新确认当前操作",
    "provider_schema_unsupported": "版权方返回结构尚未核实",
    "provider_channel_prefix_missing": "当前应用缺少已发现的渠道前缀",
    "provider_request_invalid": "当前推广配置无效",
    "provider_state_invalid": "任务恢复状态无效，需要人工核实",
    "provider_rejected": "版权方拒绝本次操作",
    "tenant_forbidden": "当前租户或操作人已停用",
    "action_forbidden": "当前操作人已无取链权限",
    "drama_not_found": "当前应用未找到完全匹配的剧目",
    "drama_ambiguous": "当前应用存在多个完全匹配的剧目，请选择候选",
    "link_episode_out_of_range": "跳转集或免费集数超出该剧可选范围，请单独调整参数",
    "link_config_invalid": "取链参数无效",
    "link_config_template_required": "付费或混合变现应用需要支付模板",
    "provider_internal_error": "当前取链步骤未完成",
}


def _error(code: str, *, retryable: bool = False) -> DomainError:
    return DomainError(code, ERROR_MESSAGES.get(code, "当前取链步骤未完成"), retryable)


def _positive(value: object) -> int:
    if isinstance(value, bool):
        raise _error("config_unverifiable")
    try:
        number = int(str(value))
    except ValueError:
        raise _error("config_unverifiable") from None
    if number < 1 or str(number) != str(value):
        raise _error("config_unverifiable")
    return number


def check_jiashu_config(existing: dict[str, Any], requested: dict[str, Any]) -> None:
    if set(requested) - {"episode"}:
        raise _error("config_unverifiable")
    wanted = _positive(requested.get("episode", 1))
    if not existing.get("jump_url"):
        return
    if existing.get("drama_num") is None:
        raise _error("config_unverifiable")
    if _positive(existing["drama_num"]) != wanted:
        raise _error("config_conflict")


def should_send(status: str) -> bool:
    return status in {"pending", "confirmed_absent"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _utc(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value)
        if (
            result.tzinfo is None
            or result.utcoffset() != timedelta(0)
            or result.isoformat() != value
        ):
            raise ValueError
        return result
    except ValueError:
        raise _error("provider_state_invalid") from None


def claim_effect(
    session: Session, *, effect_id: UUID, tenant_id: UUID, attempt_token: UUID
) -> bool:
    execution = {
        "attempt_token": str(attempt_token),
        "until": (datetime.now(UTC) + timedelta(seconds=CLAIM_SECONDS)).isoformat(),
    }
    result = session.exec(
        update(ProviderEffect)
        .where(
            col(ProviderEffect.id) == effect_id,
            col(ProviderEffect.tenant_id) == tenant_id,
            col(ProviderEffect.status).in_(["pending", "confirmed_absent"]),
        )
        .values(
            status="sending",
            attempt_token=attempt_token,
            result=col(ProviderEffect.result).op("||")({"_execution": execution}),
        )
        .returning(col(ProviderEffect.id))
    ).first()
    return result is not None


def _load(
    session: Session, context: TenantContext, item_id: UUID, *, lock: bool = False
) -> tuple[
    LinkPreparationItem, LinkPreparation, ProviderConnection, ProviderApplication
]:
    query = select(LinkPreparationItem).where(
        LinkPreparationItem.tenant_id == context.tenant_id,
        LinkPreparationItem.id == item_id,
    )
    if lock:
        query = query.with_for_update()
    item = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if item is None:
        raise DomainError("resource_not_found", "当前租户输入行不存在")
    prep = session.exec(
        select(LinkPreparation)
        .where(
            LinkPreparation.tenant_id == context.tenant_id,
            LinkPreparation.id == item.preparation_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    connection = session.exec(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == context.tenant_id,
            ProviderConnection.id == prep.connection_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    application = session.exec(
        select(ProviderApplication)
        .where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == prep.connection_id,
            ProviderApplication.external_id == prep.application_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    return item, prep, connection, application


def _authority(
    session: Session,
    context: TenantContext,
    connection: ProviderConnection,
    work: dict[str, Any],
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="provider_write",
    )
    if connection.status not in {"active", "reauth_required"}:
        raise _error(
            connection.error_code
            if connection.error_code
            in {"provider_auth_failed", "provider_session_refresh_failed"}
            else "connection_unavailable"
        )
    if work.get(
        "credential_version", connection.credential_version
    ) != connection.credential_version or work.get(
        "verification_token", str(connection.verification_token)
    ) != str(connection.verification_token):
        raise _error("provider_credentials_changed")


def _result(
    item: LinkPreparationItem,
    prep: LinkPreparation,
    connection: ProviderConnection,
    work: dict[str, Any],
    *,
    status: str,
    code: str | None = None,
    link: PromotionLink | None = None,
) -> dict[str, Any]:
    drama = work.get("drama", {})
    values = {
        "input_id": item.id,
        "line_no": item.line_no,
        "raw_input": item.raw_input,
        "provider_kind": connection.kind,
        "connection_id": connection.id,
        "application_id": prep.application_id,
        "drama_id": drama.get("id"),
        "external_drama_id": drama.get("external_drama_id"),
        "display_drama_id": drama.get("display_drama_id"),
        "title": drama.get("title"),
        "language": drama.get("language"),
        "status": status,
        "candidates": work.get("candidates", []),
        "tiktok_minis_id": work.get("tiktok_minis_id"),
        "error_code": code,
        "error_message": ERROR_MESSAGES.get(code) if code else None,
    }
    if link is not None:
        values.update(link_id=link.id, url=link.url, protected_base=link.protected_base)
    return ResolvedLink.model_validate(values).model_dump(mode="json")


def _store(
    item: LinkPreparationItem,
    prep: LinkPreparation,
    connection: ProviderConnection,
    work: dict[str, Any],
    *,
    status: str = "pending",
    code: str | None = None,
    link: PromotionLink | None = None,
) -> None:
    item.status = status
    item.resolved = {
        **_result(item, prep, connection, work, status=status, code=code, link=link),
        "_work": work,
    }


def _scope_finish(
    session: Session,
    *,
    context: TenantContext,
    item_id: UUID,
    release: bool,
) -> None:
    scope = session.exec(
        select(ProviderRemoteScope)
        .where(
            ProviderRemoteScope.tenant_id == context.tenant_id,
            ProviderRemoteScope.active_item_id == item_id,
        )
        .with_for_update()
    ).one_or_none()
    if scope is None or scope.active_item_id != item_id:
        return
    uncertain = session.exec(
        select(ProviderEffect.id)
        .where(
            ProviderEffect.tenant_id == context.tenant_id,
            ProviderEffect.remote_scope_key == scope.scope_key,
            col(ProviderEffect.status).in_(["sending", "result_unknown"]),
        )
        .limit(1)
    ).first()
    if uncertain is not None:
        scope.status = "result_unknown"
    elif release:
        scope.status, scope.active_item_id = "idle", None
    else:
        scope.status = "held"
    session.add(scope)


def _validated_work(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("provider_state_invalid")
    work = dict(value)
    if work.get("stage", "search") not in {
        "search",
        "duiba_preview",
        "ggh_detail",
        "rl_detail",
        "ml_visibility",
        "ml_sections",
        "lookup",
        "create",
        "read_before",
        "generate",
        "save",
        "verify",
        "recover_create",
        "recover_generate",
        "recover_save",
        "done",
        *STOPPED_STAGES,
    }:
        raise _error("provider_state_invalid")
    if ("claim_until" in work) != ("claim_token" in work):
        raise _error("provider_state_invalid")
    if "claim_until" in work:
        _utc(work["claim_until"])
    for key in (
        "claim_token",
        "active_effect",
        "uncertain_effect",
        "uncertain_attempt_token",
        "active_read_effect",
        "active_read_attempt_token",
    ):
        if key in work and (
            not isinstance(work[key], str) or str(UUID(work[key])) != work[key]
        ):
            raise _error("provider_state_invalid")
    if work.get("active_read_effect") and not work.get("active_read_attempt_token"):
        raise _error("provider_state_invalid")
    for key in ("search_page", "read_round"):
        if key in work and (type(work[key]) is not int or work[key] < 1):
            raise _error("provider_state_invalid")
    if not isinstance(work.get("candidates", []), list):
        raise _error("provider_state_invalid")
    for candidate in work.get("candidates", []):
        DramaCandidate.model_validate(candidate)
    if work.get("stage", "search") not in {"search", "invalid"}:
        drama = work.get("drama")
        if not isinstance(drama, dict) or str(UUID(drama["id"])) != drama["id"]:
            raise _error("provider_state_invalid")
        DramaCandidate.model_validate(
            {key: value for key, value in drama.items() if key != "id"}
        )
    return work


def _claim_unit(
    session: Session, context: TenantContext, item_id: UUID, token: UUID
) -> dict[str, Any] | None:
    item, prep, connection, application = _load(session, context, item_id, lock=True)
    if item.status in TERMINAL:
        session.rollback()
        return None
    raw_work = item.resolved.get("_work", {})
    try:
        work = _validated_work(raw_work)
    except DomainError, ValueError, TypeError, KeyError:
        # Quarantine corrupt checkpoints without guessing whether a write happened.
        # Preserve the original checkpoint for manual inspection and retain scope.
        work = {"stage": "invalid", "_invalid_work": raw_work}
    else:
        if work.get("stage") in STOPPED_STAGES:
            session.rollback()
            return None
    if work.get("claim_until") is not None:
        if _utc(work["claim_until"]) > datetime.now(UTC):
            session.rollback()
            return None
    # A crashed read may be repeated safely; retain its failed audit record and
    # advance the persisted read round, rather than treating it as an unknown write.
    if work.get("active_read_effect"):
        session.exec(
            update(ProviderEffect)
            .where(
                col(ProviderEffect.tenant_id) == context.tenant_id,
                col(ProviderEffect.id) == UUID(work.pop("active_read_effect")),
                col(ProviderEffect.step) == "read",
                col(ProviderEffect.status) == "sending",
                col(ProviderEffect.attempt_token)
                == UUID(work["active_read_attempt_token"]),
            )
            .values(status="failed", result={"error_code": "provider_unavailable"})
        )
    work.setdefault("stage", "search")
    work.setdefault("search_page", 1)
    work.setdefault("candidates", [])
    work.setdefault("credential_version", connection.credential_version)
    work.setdefault("verification_token", str(connection.verification_token))
    work["tiktok_minis_id"] = application.tiktok_minis_id
    work["claim_token"] = str(token)
    work["claim_until"] = (
        datetime.now(UTC) + timedelta(seconds=CLAIM_SECONDS)
    ).isoformat()
    _store(item, prep, connection, work)
    session.add(item)
    session.commit()
    return work


def _finish_unit(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    *,
    status: str = "pending",
    code: str | None = None,
    link: PromotionLink | None = None,
    require_authority: bool = True,
) -> bool:
    item, prep, connection, _ = _load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        return False
    if require_authority:
        _authority(session, context, connection, work)
    work.pop("claim_until", None)
    work.pop("claim_token", None)
    _store(item, prep, connection, work, status=status, code=code, link=link)
    _scope_finish(session, context=context, item_id=item_id, release=status in TERMINAL)
    session.add(item)
    session.commit()
    return True


def _persist_effect(
    session: Session,
    context: TenantContext,
    effect_id: UUID,
    token: UUID,
    *,
    status: str,
    data: dict[str, Any],
    remote_id: str | None = None,
) -> bool:
    return (
        session.exec(
            update(ProviderEffect)
            .where(
                col(ProviderEffect.tenant_id) == context.tenant_id,
                col(ProviderEffect.id) == effect_id,
                col(ProviderEffect.attempt_token) == token,
                col(ProviderEffect.status) == "sending",
            )
            .values(status=status, result=data, remote_id=remote_id)
            .returning(col(ProviderEffect.id))
        ).first()
        is not None
    )


def _page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("items"), list)
        or type(data.get("complete")) is not bool
    ):
        raise _error("provider_schema_unsupported")
    cursor = data.get("next_cursor")
    if (data["complete"] and cursor is not None) or (
        not data["complete"] and (not isinstance(cursor, str) or not cursor)
    ):
        raise _error("lookup_incomplete")
    return data["items"], cursor


# 剧事实仅存于本次 work，不进入 ProviderDrama 表。
_DRAMA_FACTS = {"free_episode_count", "episode_count"}


def stage_after_resolution(kind: str) -> str:
    """剧目确定后进入的阶段：有逐剧参数的版权方先读详情，避免候选选择后跳过。"""

    return {
        "duiba": "duiba_preview",
        "gangganhao": "ggh_detail",
        "rongliang": "rl_detail",
        "minglue": "ml_visibility",
    }.get(kind, "lookup")


def _resolved_drama(
    session: Session,
    context: TenantContext,
    prep: LinkPreparation,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    from sqlalchemy.dialects.postgresql import insert

    session.exec(
        insert(ProviderDrama)
        .values(
            id=uuid4(),
            tenant_id=context.tenant_id,
            connection_id=prep.connection_id,
            application_id=prep.application_id,
            **{
                key: value
                for key, value in candidate.items()
                if key not in _DRAMA_FACTS
            },
        )
        .on_conflict_do_update(
            index_elements=[
                "tenant_id",
                "connection_id",
                "application_id",
                "external_drama_id",
            ],
            set_={
                "title": candidate["title"],
                "language": candidate.get("language"),
                **(
                    {"display_drama_id": candidate["display_drama_id"]}
                    if candidate.get("display_drama_id")
                    else {}
                ),
            },
        )
    )
    row = session.exec(
        select(ProviderDrama).where(
            ProviderDrama.tenant_id == context.tenant_id,
            ProviderDrama.connection_id == prep.connection_id,
            ProviderDrama.application_id == prep.application_id,
            ProviderDrama.external_drama_id == candidate["external_drama_id"],
        )
    ).one()
    return {"id": str(row.id), **candidate}


def _verified(
    data: dict[str, Any], work: dict[str, Any], config: dict[str, Any]
) -> None:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("config"), dict)
        or not isinstance(data.get("attribution"), dict)
    ):
        raise _error("provider_schema_unsupported")
    if data.get("remote_id") != work["channel"]:
        raise _error("config_conflict")
    if data.get("url"):
        actual = data["config"]
        check_jiashu_config(actual, config)
        if str(actual.get("vid")) != work["drama"]["external_drama_id"]:
            raise _error("config_conflict")
        if data.get("protected_base") is None:
            raise _error("attribution_contract_unverified")


def _verified_duiba(
    data: dict[str, Any], work: dict[str, Any], config: dict[str, Any]
) -> None:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("config"), dict)
        or not isinstance(data.get("attribution"), dict)
        or data.get("remote_id") != work["channel"]
        or not data.get("url")
        or data.get("protected_base") is None
    ):
        raise _error("provider_schema_unsupported")
    actual = data["config"]
    # 卡点集按剧计算，核验以冻结在该剧 work 中的生效参数为准。
    frozen_config = work.get("duiba_config")
    expected = frozen_config if isinstance(frozen_config, dict) else config
    for key in ("episode", "card_point_episode"):
        if key in expected and _positive(actual.get(key)) != _positive(expected[key]):
            raise _error("config_conflict")
    if "miniapp_id" in expected and str(actual.get("miniapp_id")) != str(
        expected["miniapp_id"]
    ):
        raise _error("config_conflict")


def _verified_gangganhao(
    data: dict[str, Any], work: dict[str, Any], config: dict[str, Any]
) -> None:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("config"), dict)
        or not isinstance(data.get("attribution"), dict)
        or data.get("remote_id") != work["channel"]
        or not data.get("url")
        or data.get("protected_base") is None
    ):
        raise _error("provider_schema_unsupported")
    actual = data["config"]
    frozen_config = work.get("ggh_config")
    expected = frozen_config if isinstance(frozen_config, dict) else config
    for key in (
        "authorizer_app_id",
        "series_id",
        "free_episode_count",
        "episode_seq",
        "payment_template_id",
    ):
        if key in expected and str(actual.get(key)) != str(expected[key]):
            raise _error("config_conflict")


def _verified_rongliang(
    data: dict[str, Any], work: dict[str, Any], config: dict[str, Any]
) -> None:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("config"), dict)
        or not isinstance(data.get("attribution"), dict)
        or data.get("remote_id") != work["channel"]
        or not data.get("url")
        or data.get("protected_base") is None
    ):
        raise _error("provider_result_unknown")
    actual = data["config"]
    frozen_config = work.get("rongliang_config")
    expected = frozen_config if isinstance(frozen_config, dict) else config
    for key in ("client_id", "episodic_drama_id", "platform"):
        if key in expected and str(actual.get(key)) != str(expected[key]):
            raise _error("config_conflict")


def _publish(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    data: dict[str, Any],
) -> None:
    item, prep, connection, _ = _load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        return
    _authority(session, context, connection, work)
    if connection.kind == "wangyan":
        from app.modules.providers.wangyan_steps import verified_data

        data = verified_data(data, work, prep.config, prep.application_id)
    elif connection.kind == "duiba":
        _verified_duiba(data, work, prep.config)
    elif connection.kind == "gangganhao":
        _verified_gangganhao(data, work, prep.config)
    elif connection.kind == "minglue":
        from .minglue_steps import verify_data

        verify_data(data, work)
    elif connection.kind == "rongliang":
        _verified_rongliang(data, work, prep.config)
    else:
        _verified(data, work, prep.config)
    if not data.get("url"):
        raise _error("provider_result_unknown")
    # 已核实链接的归因也可补齐旧任务未保存的数字编号，技术身份不变。
    from .drama_identity import display_id

    drama_row = session.exec(
        select(ProviderDrama).where(
            ProviderDrama.id == UUID(work["drama"]["id"]),
            ProviderDrama.tenant_id == context.tenant_id,
            ProviderDrama.connection_id == prep.connection_id,
            ProviderDrama.application_id == prep.application_id,
        )
    ).one()
    drama_row.display_drama_id = display_id(
        connection.kind,
        drama_row.external_drama_id,
        drama_row.display_drama_id,
        data.get("attribution"),
    )
    work["drama"]["display_drama_id"] = drama_row.display_drama_id
    session.add(drama_row)
    key = link_reuse_key(
        context.tenant_id,
        prep.connection_id,
        prep.application_id,
        work["drama"]["external_drama_id"],
        prep.config,
    )
    versions = session.exec(
        select(PromotionLink)
        .where(
            PromotionLink.tenant_id == context.tenant_id, PromotionLink.reuse_key == key
        )
        .order_by(col(PromotionLink.version).desc())
        .with_for_update()
    ).all()
    current = next((row for row in versions if row.status == "ready"), None)
    if (
        current is not None
        and current.url == data["url"]
        and current.protected_base == data["protected_base"]
    ):
        link = current
    else:
        if current is not None:
            current.status = "superseded"
            session.add(current)
            session.flush()
        link = PromotionLink(
            tenant_id=context.tenant_id,
            reuse_key=key,
            drama_id=UUID(work["drama"]["id"]),
            connection_id=prep.connection_id,
            application_id=prep.application_id,
            config=prep.config,
            remote_id=data["remote_id"],
            url=data["url"],
            protected_base=data["protected_base"],
            attribution=data["attribution"],
            version=versions[0].version + 1 if versions else 1,
            verified_at=datetime.now(UTC),
            status="ready",
        )
        session.add(link)
        session.flush()
    work["stage"] = "done"
    _finish_unit(session, context, item_id, token, work, status="ready", link=link)


def _effect_request(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    step: str,
    payload: dict[str, Any],
    client: ProviderClient,
) -> dict[str, Any]:
    digest = _digest(
        [step, payload, work["credential_version"], work["verification_token"]]
    )
    effect = get_or_create_effect(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        step=step,
        request_digest=digest,
    )
    if effect.status == "succeeded":
        data = effect.result.get("data", {})
        if not isinstance(data, dict):
            raise _error("provider_state_invalid")
        work.pop("active_effect", None)
        return data
    if effect.status == "failed":
        raise _error(effect.result.get("error_code", "provider_rejected"))
    if not should_send(effect.status):
        work["uncertain_effect"] = str(effect.id)
        work["uncertain_attempt_token"] = str(effect.attempt_token)
        work["stage"] = "recover_" + step
        raise _error("provider_result_unknown")
    effect_id = effect.id
    if not claim_effect(
        session, effect_id=effect_id, tenant_id=context.tenant_id, attempt_token=token
    ):
        raise _error("provider_result_unknown")
    # Persist the exact effect identity before the request; recovery can identify
    # a sending effect even when the process dies before receiving a response.
    item, prep, connection, _ = _load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        raise _error("provider_scope_busy")
    _authority(session, context, connection, work)
    work["active_effect"] = str(effect_id)
    _store(item, prep, connection, work)
    session.add(item)
    session.commit()
    try:
        response = client.create_step(step, payload)
    except Exception as error:
        code = (
            error.code if isinstance(error, DomainError) else "provider_result_unknown"
        )
        unknown = code in {
            "provider_result_unknown",
            "provider_unavailable",
            "provider_schema_unsupported",
            "provider_session_expired",
        }
        _persist_effect(
            session,
            context,
            effect_id,
            token,
            status="result_unknown" if unknown else "failed",
            data={"error_code": code},
        )
        session.commit()
        if unknown:
            work["uncertain_effect"] = str(effect_id)
            work["uncertain_attempt_token"] = str(token)
            work["stage"] = "recover_" + step
        raise _error(code) from None
    item, _, connection, _ = _load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        raise _error("provider_scope_busy")
    if not _persist_effect(
        session,
        context,
        effect_id,
        token,
        status="succeeded",
        data={"data": response},
        remote_id=work["channel"],
    ):
        session.rollback()
        raise _error("provider_scope_busy")
    # Preserve a definite external acknowledgement even if authority changes
    # before the next step. Promotion still performs a fresh permission check.
    session.commit()
    work.pop("active_effect", None)
    return response


def _run_duiba_item(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
    client: ProviderClient,
    connection_id: UUID,
    application_id: str,
    raw_input: str,
) -> None:
    """Advance one 兑吧 step while preserving the durable effect boundary."""

    if not isinstance(client, DuibaClient):
        raise _error("provider_state_invalid")
    stage = work["stage"]
    if stage == "search":
        cursor = None if work["search_page"] == 1 else str(work["search_page"])
        page = client.search(raw_input.strip(), cursor)
        candidates = {row["external_drama_id"]: row for row in work["candidates"]}
        for candidate in page.items:
            row = candidate.model_dump()
            previous = candidates.get(row["external_drama_id"])
            if previous is not None and previous != row:
                raise _error("provider_schema_unsupported")
            candidates[row["external_drama_id"]] = row
        work["candidates"] = list(candidates.values())
        if not page.complete:
            assert page.next_cursor is not None
            work["search_page"] = _positive(page.next_cursor)
        elif len(candidates) != 1:
            _finish_unit(
                session,
                context,
                item_id,
                token,
                work,
                status="needs_resolution",
                code="drama_not_found" if not candidates else "drama_ambiguous",
            )
            return
        else:
            _, prep, _, _ = _load(session, context, item_id)
            work["drama"] = _resolved_drama(
                session, context, prep, next(iter(candidates.values()))
            )
            work["stage"] = stage_after_resolution("duiba")
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "duiba_preview":
        drama_id = work["drama"]["external_drama_id"]
        episodes = client.preview_drama(drama_id)
        episode = _positive(config.get("episode", 1))
        # 卡点集未调整时与后台/CLI 一致取剧免费集数；剧单缺失时回退为起播集。
        card_point = _positive(
            config.get("card_point_episode")
            or work["drama"].get("free_episode_count")
            or episode
        )
        by_serial = {row["serialNo"]: row for row in episodes}
        if episode not in by_serial or card_point not in by_serial:
            raise _error("provider_request_invalid")
        effective = dict(config)
        effective.update(card_point_episode=card_point, episode=episode)
        effective.setdefault("miniapp_id", application_id)
        effective.update(
            default_episode_lc_id=by_serial[episode]["lc671EpisodeId"],
            card_point_episode_lc_id=by_serial[card_point]["lc671EpisodeId"],
        )
        work["duiba_config"] = effective
        work["stage"] = "lookup"
        work["lookup_cursor"] = None
        work["lookup_found"] = None
        _finish_unit(session, context, item_id, token, work)
        return

    effective = work.get("duiba_config")
    if not isinstance(effective, dict):
        raise _error("provider_state_invalid")
    scope_key = _digest(
        [
            str(context.tenant_id),
            str(connection_id),
            application_id,
            "duiba",
            work["drama"]["external_drama_id"],
            effective.get("miniapp_id"),
            effective.get("episode"),
            effective.get("card_point_episode"),
        ]
    )
    work["scope_key"] = scope_key
    work.setdefault(
        "channel",
        "duiba-"
        + _digest(
            [
                work["drama"]["external_drama_id"],
                effective.get("miniapp_id"),
                effective.get("episode"),
                effective.get("card_point_episode"),
            ]
        ),
    )
    if not claim_remote_scope(
        session, tenant_id=context.tenant_id, scope_key=scope_key, item_id=item_id
    ):
        session.commit()
        raise _error("provider_scope_busy", retryable=True)
    session.commit()

    if stage in {"lookup", "recover_create"}:
        try:
            page = client.lookup_link(
                work["drama"]["external_drama_id"],
                effective,
                work.get("lookup_cursor"),
            )
        except DomainError as error:
            if (
                error.code == "provider_unavailable"
                and stage == "lookup"
                and client.safe_idempotent_create_without_lookup
            ):
                work["lookup_complete"] = True
                work["lookup_unavailable"] = True
                work["stage"] = "create"
                _finish_unit(session, context, item_id, token, work)
                return
            raise
        for row in page.items:
            remote_id = row.get("linkNo", row.get("id"))
            if remote_id is not None:
                work["channel"] = str(remote_id)
                work["lookup_found"] = str(remote_id)
        if not page.complete:
            work["lookup_cursor"] = page.next_cursor
            _finish_unit(session, context, item_id, token, work)
            return
        if stage == "recover_create":
            _recover_effect(
                session, context, work, found=work.get("lookup_found") is not None
            )
        work["lookup_complete"] = True
        work["stage"] = "read_before" if work.get("lookup_found") else "create"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "create":
        if not work.get("lookup_complete"):
            raise _error("lookup_incomplete")
        response = _effect_request(
            session,
            context,
            item_id,
            token,
            work,
            "create",
            {"vid": work["drama"]["external_drama_id"], "config": effective},
            client,
        )
        remote_id = response.get("remote_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise _error("provider_result_unknown")
        work["channel"] = remote_id
        work["stage"] = "verify"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage in {"read_before", "verify"}:
        data = _read_effect(session, context, item_id, token, work, client)
        _verified_duiba(data, work, config)
        _publish(session, context, item_id, token, work, data)
        return

    raise _error("provider_state_invalid")


def _run_gangganhao_item(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
    client: ProviderClient,
    connection_id: UUID,
    application_id: str,
    raw_input: str,
) -> None:
    """Advance one 刚刚好 step, including exact payment-template matching."""

    if not isinstance(client, GangganhaoClient):
        raise _error("provider_state_invalid")
    stage = work["stage"]
    if stage == "search":
        cursor = None if work["search_page"] == 1 else str(work["search_page"])
        page = client.search(raw_input.strip(), cursor)
        candidates = {row["external_drama_id"]: row for row in work["candidates"]}
        for candidate in page.items:
            row = candidate.model_dump()
            previous = candidates.get(row["external_drama_id"])
            if previous is not None and previous != row:
                raise _error("provider_schema_unsupported")
            candidates[row["external_drama_id"]] = row
        work["candidates"] = list(candidates.values())
        if not page.complete:
            assert page.next_cursor is not None
            work["search_page"] = _positive(page.next_cursor)
        elif len(candidates) != 1:
            _finish_unit(
                session,
                context,
                item_id,
                token,
                work,
                status="needs_resolution",
                code="drama_not_found" if not candidates else "drama_ambiguous",
            )
            return
        else:
            _, prep, _, _ = _load(session, context, item_id)
            work["drama"] = _resolved_drama(
                session, context, prep, next(iter(candidates.values()))
            )
            work["stage"] = stage_after_resolution("gangganhao")
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "ggh_detail":
        drama = work["drama"]
        detail = client.series_detail(drama["external_drama_id"])
        # 免费集数未调整时取剧自身设置，缺失时为 3，与 CLI 一致。
        free = config.get("free_episode_count")
        if free is None:
            free = drama.get("free_episode_count")
        free = 3 if free is None else int(free)
        seq = _positive(config.get("episode_seq", 1))
        # 校验与刚刚好后台一致；不满足时只让该剧冲突，不静默改值。
        if not episode_range_valid(free, seq, drama.get("episode_count")):
            raise _error("link_episode_out_of_range")
        effective = dict(config)
        delivery_mode = detail.get("delivery_mode")
        if not isinstance(delivery_mode, str) or not delivery_mode.strip():
            delivery_mode = getattr(client, "delivery_mode", "iaa")
        effective.update(
            free_episode_count=free,
            episode_seq=seq,
            authorizer_app_id=application_id,
            series_id=detail["series_id"],
            series_title=detail["series_title"],
            delivery_mode=delivery_mode,
        )
        work["ggh_config"] = effective
        work["stage"] = "lookup"
        work["lookup_cursor"] = None
        work["lookup_found"] = None
        _finish_unit(session, context, item_id, token, work)
        return

    effective = work.get("ggh_config")
    if not isinstance(effective, dict):
        raise _error("provider_state_invalid")
    work["scope_key"] = _digest(
        [
            str(context.tenant_id),
            str(connection_id),
            application_id,
            "gangganhao",
            work["drama"]["external_drama_id"],
            effective.get("free_episode_count"),
            effective.get("episode_seq"),
            effective.get("payment_template_id"),
        ]
    )
    work.setdefault(
        "channel",
        "ggh-"
        + _digest(
            [
                work["drama"]["external_drama_id"],
                effective.get("free_episode_count"),
                effective.get("episode_seq"),
                effective.get("payment_template_id"),
            ]
        ),
    )
    if not claim_remote_scope(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        item_id=item_id,
    ):
        session.commit()
        raise _error("provider_scope_busy", retryable=True)
    session.commit()

    if stage in {"lookup", "recover_create"}:
        page = client.lookup_link(
            work["drama"]["external_drama_id"],
            effective,
            work.get("lookup_cursor"),
        )
        for row in page.items:
            remote_id = row.get("id", row.get("linkId"))
            if remote_id is not None:
                work["channel"] = str(remote_id)
                work["lookup_found"] = str(remote_id)
        if not page.complete:
            work["lookup_cursor"] = page.next_cursor
            _finish_unit(session, context, item_id, token, work)
            return
        if stage == "recover_create":
            _recover_effect(
                session, context, work, found=work.get("lookup_found") is not None
            )
        work["lookup_complete"] = True
        work["stage"] = "read_before" if work.get("lookup_found") else "create"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "create":
        if not work.get("lookup_complete"):
            raise _error("lookup_incomplete")
        response = _effect_request(
            session,
            context,
            item_id,
            token,
            work,
            "create",
            {"vid": work["drama"]["external_drama_id"], "config": effective},
            client,
        )
        remote_id = response.get("remote_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise _error("provider_result_unknown")
        work["channel"] = remote_id
        work["stage"] = "verify"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage in {"read_before", "verify"}:
        data = _read_effect(session, context, item_id, token, work, client)
        _verified_gangganhao(data, work, config)
        _publish(session, context, item_id, token, work, data)
        return

    raise _error("provider_state_invalid")


def _run_rongliang_item(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
    client: ProviderClient,
    connection_id: UUID,
    application_id: str,
    raw_input: str,
) -> None:
    """Advance one 容量 step and require an exact post-create batch read-back."""

    if not isinstance(client, RongliangClient):
        raise _error("provider_state_invalid")
    stage = work["stage"]
    if stage == "search":
        cursor = None if work["search_page"] == 1 else str(work["search_page"])
        page = client.search(raw_input.strip(), cursor)
        candidates = {row["external_drama_id"]: row for row in work["candidates"]}
        for candidate in page.items:
            row = candidate.model_dump()
            previous = candidates.get(row["external_drama_id"])
            if previous is not None and previous != row:
                raise _error("provider_schema_unsupported")
            candidates[row["external_drama_id"]] = row
        work["candidates"] = list(candidates.values())
        if not page.complete:
            assert page.next_cursor is not None
            work["search_page"] = _positive(page.next_cursor)
        elif len(candidates) != 1:
            _finish_unit(
                session,
                context,
                item_id,
                token,
                work,
                status="needs_resolution",
                code="drama_not_found" if not candidates else "drama_ambiguous",
            )
            return
        else:
            _, prep, _, _ = _load(session, context, item_id)
            work["drama"] = _resolved_drama(
                session, context, prep, next(iter(candidates.values()))
            )
            work["stage"] = stage_after_resolution("rongliang")
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "rl_detail":
        compilation_id = work["drama"]["external_drama_id"]
        options = client.episode_options(compilation_id)
        if not options:
            raise _error("provider_request_invalid")
        # 后台网页固定选剧集下拉第一项，不开放其他起播集；这里保持一致。
        episode_id = options[0].get("episodicDramaId")
        if episode_id is None:
            raise _error("provider_schema_unsupported")
        effective = dict(config)
        effective.update(
            client_id=application_id,
            episodic_drama_id=episode_id,
            platform=1,
            delivery_type=1,
            link_type=2,
            alias=client.compilations_alias(compilation_id),
        )
        work["rongliang_config"] = effective
        work["stage"] = "lookup"
        work["lookup_cursor"] = None
        work["lookup_found"] = None
        _finish_unit(session, context, item_id, token, work)
        return

    effective = work.get("rongliang_config")
    if not isinstance(effective, dict):
        raise _error("provider_state_invalid")
    work["scope_key"] = _digest(
        [
            str(context.tenant_id),
            str(connection_id),
            application_id,
            "rongliang",
            work["drama"]["external_drama_id"],
            effective.get("client_id"),
            effective.get("episodic_drama_id"),
            effective.get("platform"),
        ]
    )
    work.setdefault(
        "channel",
        "rl-"
        + _digest(
            [
                work["drama"]["external_drama_id"],
                effective.get("client_id"),
                effective.get("episodic_drama_id"),
                effective.get("platform"),
            ]
        ),
    )
    if not claim_remote_scope(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        item_id=item_id,
    ):
        session.commit()
        raise _error("provider_scope_busy", retryable=True)
    session.commit()

    if stage in {"lookup", "recover_create"}:
        page = client.lookup_link(
            work["drama"]["external_drama_id"],
            effective,
            work.get("lookup_cursor"),
        )
        for row in page.items:
            batch = row.get("batchId")
            if isinstance(batch, str) and batch:
                work["channel"] = batch
                work["lookup_found"] = batch
        if not page.complete:
            work["lookup_cursor"] = page.next_cursor
            _finish_unit(session, context, item_id, token, work)
            return
        if stage == "recover_create":
            _recover_effect(
                session, context, work, found=work.get("lookup_found") is not None
            )
        work["lookup_complete"] = True
        work["stage"] = "read_before" if work.get("lookup_found") else "create"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "create":
        if not work.get("lookup_complete"):
            raise _error("lookup_incomplete")
        response = _effect_request(
            session,
            context,
            item_id,
            token,
            work,
            "create",
            {"vid": work["drama"]["external_drama_id"], "config": effective},
            client,
        )
        remote_id = response.get("remote_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise _error("provider_result_unknown")
        work["channel"] = remote_id
        work["stage"] = "verify"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage in {"read_before", "verify"}:
        data = _read_effect(session, context, item_id, token, work, client)
        _verified_rongliang(data, work, config)
        _publish(session, context, item_id, token, work, data)
        return

    raise _error("provider_state_invalid")


def run_link_item(
    session: Session,
    *,
    context: TenantContext,
    item_id: UUID,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Run one request unit; rescheduling belongs to the transactional task layer."""
    token = uuid4()
    work: dict[str, Any] = {}
    kind: str | None = None
    try:
        claimed = _claim_unit(session, context, item_id, token)
        if claimed is None:
            return
        work = claimed
        if work["stage"] == "invalid":
            raise _error("provider_state_invalid")
        item, prep, connection, _ = _load(session, context, item_id)
        kind = connection.kind
        _authority(session, context, connection, work)
        stage = work["stage"]
        tenant_name = ""
        if kind == "jiashu" and stage == "create":
            tenant = session.get(Tenant, context.tenant_id, populate_existing=True)
            if tenant is None:
                raise _error("tenant_forbidden")
            tenant_name = tenant.name
        # Close snapshot transaction before the factory/HTTP call; no database
        # row lock spans network I/O. Factory rechecks live tenant/app authority.
        connection_id, application_id, kind, raw_input, config = (
            prep.connection_id,
            prep.application_id,
            connection.kind,
            item.raw_input,
            dict(prep.config),
        )
        session.commit()
        from app.modules.providers.connections import open_provider_session

        bind = session.get_bind()
        database_engine = getattr(bind, "engine", bind)
        with open_provider_session(
            database_engine=database_engine,
            context=context,
            connection_id=connection_id,
            application_id=application_id,
            action="provider_write",
            transport=transport,
        ) as provider:
            client = provider.client
            if kind == "minglue":
                from .minglue_steps import run_item

                run_item(
                    session,
                    context,
                    item_id,
                    token,
                    work,
                    config,
                    client,
                    connection_id,
                    application_id,
                    raw_input,
                )
                return
            if kind == "duiba":
                _run_duiba_item(
                    session,
                    context,
                    item_id,
                    token,
                    work,
                    config,
                    client,
                    connection_id,
                    application_id,
                    raw_input,
                )
                return
            if kind == "gangganhao":
                _run_gangganhao_item(
                    session,
                    context,
                    item_id,
                    token,
                    work,
                    config,
                    client,
                    connection_id,
                    application_id,
                    raw_input,
                )
                return
            if kind == "rongliang":
                _run_rongliang_item(
                    session,
                    context,
                    item_id,
                    token,
                    work,
                    config,
                    client,
                    connection_id,
                    application_id,
                    raw_input,
                )
                return
            from app.modules.providers.adapters.jiashu import JiashuClient
            from app.modules.providers.adapters.wangyan import WangyanClient

            if not isinstance(client, (JiashuClient, WangyanClient)):
                raise _error("provider_state_invalid")
            if stage != "search" and kind == "jiashu":
                local = find_ready_link(
                    session,
                    context=context,
                    connection_id=connection_id,
                    application_id=application_id,
                    external_drama_id=work["drama"]["external_drama_id"],
                    config=config,
                )
                if (
                    local is not None
                    and not work.get("uncertain_effect")
                    and not work.get("active_effect")
                ):
                    _finish_unit(
                        session,
                        context,
                        item_id,
                        token,
                        work,
                        status="ready",
                        link=local,
                    )
                    return
                session.commit()
            if kind == "wangyan":
                from app.modules.providers.wangyan_steps import advance, guard

                if not isinstance(client, WangyanClient):
                    raise _error("provider_state_invalid")
                if stage != "search":
                    data = advance(
                        session,
                        context,
                        item_id,
                        token,
                        work,
                        config,
                        client,
                        connection_id,
                        application_id,
                    )
                    if data is not None:
                        _publish(session, context, item_id, token, work, data)
                    else:
                        _finish_unit(session, context, item_id, token, work)
                    return
                guard(session, context, item_id, token, work)
                session.commit()
            if stage == "search":
                rows, cursor = _page(
                    client.search(raw_input.strip(), page=work["search_page"])
                )
                candidates = {
                    row["external_drama_id"]: row for row in work["candidates"]
                }
                for row in rows:
                    candidate = DramaCandidate.model_validate(row).model_dump()
                    if candidate["title"] == raw_input.strip():
                        previous = candidates.get(candidate["external_drama_id"])
                        if previous is not None and previous != candidate:
                            raise _error("provider_schema_unsupported")
                        candidates[candidate["external_drama_id"]] = candidate
                work["candidates"] = list(candidates.values())
                if cursor is not None:
                    page = _positive(cursor)
                    if page <= work["search_page"]:
                        raise _error("lookup_incomplete")
                    work["search_page"] = page
                elif len(candidates) != 1:
                    _finish_unit(
                        session,
                        context,
                        item_id,
                        token,
                        work,
                        status="needs_resolution",
                        code="drama_not_found" if not candidates else "drama_ambiguous",
                    )
                    return
                else:
                    _, prep, _, _ = _load(session, context, item_id)
                    work["drama"] = _resolved_drama(
                        session, context, prep, next(iter(candidates.values()))
                    )
                    work["stage"] = "lookup"
                    work["lookup_cursor"] = None
                    work["lookup_found"] = None
            else:
                if kind != "jiashu":
                    raise _error("provider_state_invalid")
                if not isinstance(client, JiashuClient):
                    raise _error("provider_state_invalid")
                channel_for = client.channel_for
                channel = channel_for(work["drama"]["external_drama_id"])
                if work.get("channel", channel) != channel:
                    raise _error("provider_credentials_changed")
                work["channel"] = channel
                work["scope_key"] = _digest(
                    [
                        str(context.tenant_id),
                        str(connection_id),
                        application_id,
                        "jiashu",
                        channel,
                    ]
                )
                if not claim_remote_scope(
                    session,
                    tenant_id=context.tenant_id,
                    scope_key=work["scope_key"],
                    item_id=item_id,
                ):
                    session.commit()
                    raise _error("provider_scope_busy", retryable=True)
                session.commit()
                check_jiashu_config({}, config)
                if stage in {"lookup", "recover_create"}:
                    rows, cursor = _page(
                        client.find_existing(
                            work["drama"]["external_drama_id"],
                            config,
                            work.get("lookup_cursor"),
                        )
                    )
                    for row in rows:
                        if row.get("remote_id") != channel:
                            raise _error("provider_schema_unsupported")
                        work["lookup_found"] = channel
                    previous_cursor = work.get("lookup_cursor")
                    if cursor is not None and _positive(cursor) <= (
                        _positive(previous_cursor) if previous_cursor else 1
                    ):
                        raise _error("lookup_incomplete")
                    work["lookup_cursor"] = cursor
                    if cursor is None:
                        if stage == "recover_create":
                            _recover_effect(
                                session,
                                context,
                                work,
                                found=work.get("lookup_found") is not None,
                            )
                        work["stage"] = (
                            "read_before" if work.get("lookup_found") else "create"
                        )
                        work["lookup_complete"] = True
                elif stage in {"create", "generate", "save"}:
                    if not work.get("lookup_complete"):
                        raise _error("lookup_incomplete")
                    payload = {
                        "channel": channel,
                        "vid": work["drama"]["external_drama_id"],
                    }
                    if stage == "create":
                        # 嘉书备注用于后台人工追溯；渠道匹配仍只依赖渠道前缀和剧目 ID。
                        payload["remark"] = (
                            f"{work['drama']['external_drama_id']}-"
                            f"{work['drama']['title']}-{tenant_name}"
                        )
                    else:
                        payload.update(
                            drama_num=_positive(config.get("episode", 1)),
                            existing_config=work.get("existing_config", {}),
                        )
                        if stage == "save":
                            payload.update(
                                jump_url=work["generated"]["url"],
                                minis_path=work["generated"]["minis_path"],
                            )
                    response = _effect_request(
                        session, context, item_id, token, work, stage, payload, client
                    )
                    if stage == "generate":
                        work["generated"] = response
                    work["stage"] = {
                        "create": "read_before",
                        "generate": "save",
                        "save": "verify",
                    }[stage]
                elif stage in {
                    "read_before",
                    "verify",
                    "recover_generate",
                    "recover_save",
                }:
                    data = _read_effect(session, context, item_id, token, work, client)
                    # Preserve parsed business settings for conflict diagnosis;
                    # public projection exposes only allowlisted scalar fields.
                    work["existing_config"] = data.get("config", {})
                    _verified(data, work, config)
                    if data.get("url"):
                        if stage.startswith("recover_"):
                            _recover_effect(session, context, work, found=True)
                        _publish(session, context, item_id, token, work, data)
                        return
                    if stage == "read_before":
                        work["existing_config"] = data["config"]
                        work["stage"] = "generate"
                    else:
                        # Empty saved URL cannot prove an unknown generation/save
                        # did not happen. Keep the scope until authoritative proof.
                        raise _error("provider_result_unknown")
                else:
                    raise _error("provider_state_invalid")
            _finish_unit(session, context, item_id, token, work)
    except Exception as error:
        session.rollback()
        code = (
            error.code
            if isinstance(error, DomainError) and error.code in ERROR_MESSAGES
            else "provider_internal_error"
        )
        status = (
            "blocked_auth"
            if code
            in {
                "tenant_forbidden",
                "action_forbidden",
                "provider_session_expired",
                "provider_application_forbidden",
                "provider_credentials_changed",
                "provider_auth_failed",
                "provider_session_refresh_failed",
                "connection_unavailable",
            }
            else "config_conflict"
            if code
            in {"config_conflict", "config_unverifiable", "link_episode_out_of_range"}
            else "result_unknown"
            if code in {"provider_result_unknown", "provider_state_invalid"}
            else "retryable_error"
            if code in {"provider_unavailable", "provider_session_refreshing"}
            else "pending"
            if code == "provider_scope_busy"
            else "failed"
        )
        if (
            kind in {"wangyan", "duiba", "gangganhao", "rongliang", "minglue"}
            and code
            not in {
                "provider_rejected",
                "config_conflict",
                "provider_auth_failed",
                "provider_session_refresh_failed",
                "connection_unavailable",
                "provider_application_forbidden",
                "provider_credentials_changed",
            }
            and (
                work.get("active_effect")
                or work.get("uncertain_effect")
                or work.get("remote_id")
            )
        ):
            # Known effects keep read-only recovery through local failures too.
            # Every next HTTP still requires the original actor and credentials.
            status = "result_unknown"
            if work.get("stage") == "done":
                work["stage"] = "verify"
        # Persist only application-owned codes/text. No raw exception or response.
        _finish_unit(
            session,
            context,
            item_id,
            token,
            work,
            status=status,
            code=code,
            require_authority=False,
        )


def _read_effect(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    client: ProviderClient,
) -> dict[str, Any]:
    work["read_round"] = work.get("read_round", 0) + 1
    effect = get_or_create_effect(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        step="read",
        request_digest=_digest(
            [work["stage"], work["channel"], str(item_id), work["read_round"]]
        ),
    )
    effect_id = effect.id
    if not claim_effect(
        session, effect_id=effect_id, tenant_id=context.tenant_id, attempt_token=token
    ):
        raise _error("provider_scope_busy")
    item, prep, connection, _ = _load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        raise _error("provider_scope_busy")
    _authority(session, context, connection, work)
    work["active_read_effect"] = str(effect_id)
    work["active_read_attempt_token"] = str(token)
    _store(item, prep, connection, work)
    session.add(item)
    session.commit()
    try:
        data = client.read_link(work["channel"])
        if isinstance(data, LinkReceipt):
            data = asdict(data)
    except Exception:
        _persist_effect(
            session,
            context,
            effect_id,
            token,
            status="failed",
            data={"error_code": "provider_unavailable"},
        )
        session.commit()
        work.pop("active_read_effect", None)
        raise
    if not _persist_effect(
        session,
        context,
        effect_id,
        token,
        status="succeeded",
        data={"data": data},
        remote_id=work["channel"],
    ):
        session.rollback()
        raise _error("provider_scope_busy")
    session.commit()
    work.pop("active_read_effect", None)
    return data


def _recover_effect(
    session: Session, context: TenantContext, work: dict[str, Any], *, found: bool
) -> None:
    effect = session.exec(
        select(ProviderEffect)
        .where(
            ProviderEffect.tenant_id == context.tenant_id,
            ProviderEffect.id == UUID(work["uncertain_effect"]),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if effect.status not in {"sending", "result_unknown"} or str(
        effect.attempt_token
    ) != work.get("uncertain_attempt_token"):
        raise _error("provider_state_invalid")
    effect.status = "succeeded" if found else "confirmed_absent"
    effect.remote_id = work["channel"] if found else None
    effect.result = {"recovered": True, "found": found}
    session.add(effect)
    work.pop("uncertain_effect", None)
    work.pop("uncertain_attempt_token", None)
    work.pop("active_effect", None)
