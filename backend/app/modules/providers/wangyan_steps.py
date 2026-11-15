"""Wangyan single-request units: durable history, one create, positive readback.

The existing provider claim and remote scope fence remain authoritative. Lookup
observations use indexed effect keys, so a 200k-row history never grows an item
checkpoint or a Python collection beyond one 20-row page.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.providers import link_steps as core
from app.modules.providers.adapters.wangyan import WangyanClient, render_attribution
from app.modules.providers.models import ProviderEffect
from app.modules.providers.repository import claim_remote_scope, get_or_create_effect


def _identity(value: object) -> str:
    if not isinstance(value, str) or len(value) > 255:
        raise core._error("config_unverifiable")
    return str(core._positive(value))


def guard(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
) -> None:
    """Fresh original actor, connection, app and claim immediately before HTTP."""
    item, prep, connection, application = core._load(
        session, context, item_id, lock=True
    )
    current = item.resolved.get("_work", {})
    if current.get("claim_token") != str(token) or core._utc(
        current["claim_until"]
    ) <= datetime.now(UTC):
        raise core._error("provider_scope_busy")
    if prep.actor_id != context.actor_id:
        raise core._error("action_forbidden")
    core._authority(session, context, connection, work)
    if connection.kind != "wangyan" or application.channel_config.get(
        "verification_token"
    ) != str(connection.verification_token):
        raise core._error("provider_application_forbidden")


def _checkpoint(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
) -> None:
    # Network observations remain facts if authorization changed during HTTP.
    # Publishing a link and every later request still recheck current authority.
    item, prep, connection, _ = core._load(session, context, item_id, lock=True)
    if item.resolved.get("_work", {}).get("claim_token") != str(token):
        session.rollback()
        raise core._error("provider_scope_busy")
    core._store(item, prep, connection, work)
    session.add(item)
    session.commit()


def _reset_scan(work: dict[str, Any]) -> None:
    work["wy_scan"] = {
        "id": str(uuid4()),
        "cursor": None,
        "total": None,
        "seen": 0,
        "pages": 0,
        "matches": 0,
        "candidate": None,
        "usable": False,
        "done": False,
    }


def _scan(work: dict[str, Any]) -> dict[str, Any]:
    if "wy_scan" not in work:
        _reset_scan(work)
    scan = work["wy_scan"]
    try:
        if not isinstance(scan, dict) or str(UUID(scan["id"])) != scan["id"]:
            raise ValueError
        if scan["cursor"] is not None and (
            not isinstance(scan["cursor"], str) or len(scan["cursor"]) > 1024
        ):
            raise ValueError
        for key in ("seen", "pages", "matches"):
            if type(scan[key]) is not int or not 0 <= scan[key] <= 200_000:
                raise ValueError
        if scan["total"] is not None and (
            type(scan["total"]) is not int or not 0 <= scan["total"] <= 200_000
        ):
            raise ValueError
        if type(scan["done"]) is not bool or type(scan["usable"]) is not bool:
            raise ValueError
        if scan["candidate"] is not None:
            _identity(scan["candidate"])
    except ValueError, TypeError, KeyError, DomainError:
        raise core._error("provider_state_invalid") from None
    return scan


def _read(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    request: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    guard(session, context, item_id, token, work)
    work["read_round"] = work.get("read_round", 0) + 1
    effect = get_or_create_effect(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        step="read",
        request_digest=core._digest(
            [
                "wangyan",
                str(item_id),
                work["stage"],
                work["read_round"],
                work.get("remote_id"),
                work.get("wy_scan"),
            ]
        ),
    )
    identity = effect.id
    if not core.claim_effect(
        session, effect_id=identity, tenant_id=context.tenant_id, attempt_token=token
    ):
        raise core._error("provider_scope_busy")
    work["active_read_effect"], work["active_read_attempt_token"] = (
        str(identity),
        str(token),
    )
    _checkpoint(session, context, item_id, token, work)
    try:
        data = request()
    except Exception:
        core._persist_effect(
            session,
            context,
            identity,
            token,
            status="failed",
            data={"error_code": "provider_unavailable"},
        )
        session.commit()
        work.pop("active_read_effect", None)
        work.pop("active_read_attempt_token", None)
        raise
    if not core._persist_effect(
        session,
        context,
        identity,
        token,
        status="succeeded",
        data={"data": data},
        remote_id=work.get("remote_id"),
    ):
        session.rollback()
        raise core._error("provider_scope_busy")
    session.commit()
    work.pop("active_read_effect", None)
    work.pop("active_read_attempt_token", None)
    return data


def _observed(
    session: Session,
    context: TenantContext,
    work: dict[str, Any],
    scan: dict[str, Any],
    identities: list[str],
) -> None:
    page_key = core._digest([scan["id"], scan["cursor"]])
    for identity in identities:
        row = get_or_create_effect(
            session,
            tenant_id=context.tenant_id,
            scope_key=work["scope_key"],
            step="wy_seen",
            request_digest=core._digest([scan["id"], identity]),
        )
        if row.status == "succeeded":
            if row.result != {"scan_id": scan["id"], "page": page_key, "id": identity}:
                raise core._error("lookup_incomplete")
        elif row.status != "pending":
            raise core._error("provider_state_invalid")
        else:
            row.status = "succeeded"
            row.remote_id = identity
            row.result = {"scan_id": scan["id"], "page": page_key, "id": identity}
            session.add(row)


def _creation(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
) -> ProviderEffect:
    return get_or_create_effect(
        session,
        tenant_id=context.tenant_id,
        scope_key=work["scope_key"],
        step="create",
        request_digest=core._digest(
            [
                "wangyan",
                str(item_id),
                work["drama"]["external_drama_id"],
                config,
                work["credential_version"],
                work["verification_token"],
            ]
        ),
    )


def _recovery_effect(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
) -> ProviderEffect:
    # 行锁读取：恢复判定与回执写入串行，状态检查基于最新提交值。
    identity = _creation(session, context, item_id, work, config).id
    effect = session.exec(
        select(ProviderEffect)
        .where(ProviderEffect.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if (
        effect.status not in {"sending", "result_unknown", "succeeded"}
        or effect.attempt_token is None
        or work.get("uncertain_effect") != str(effect.id)
        or work.get("uncertain_attempt_token") != str(effect.attempt_token)
        or work.get("created_effect") != str(effect.id)
    ):
        raise core._error("provider_state_invalid")
    return effect


def _confirm_absent(
    session: Session,
    context: TenantContext,
    work: dict[str, Any],
    config: dict[str, Any],
    item_id: UUID,
) -> None:
    """建链前已完整确认无同参数链接，恢复扫描仍为 0 条说明那次请求未落地。"""
    effect = _recovery_effect(session, context, item_id, work, config)
    if effect.status == "result_unknown":
        # 网眼已明确受理（accepted）却查不到链接，不能视为未落地，只能人工核实。
        absent = effect.result.get("accepted") is False
    elif effect.status == "sending":
        # 发送方进程中断、没有任何回执：原尝试的执行期限过后，请求方才不可能仍在等待响应。
        execution = effect.result.get("_execution")
        absent = (
            isinstance(execution, dict)
            and isinstance(execution.get("until"), str)
            and core._utc(execution["until"]) <= datetime.now(UTC)
        )
    else:
        absent = False
    if not absent or _known(session, context, effect) is not None:
        raise core._error("provider_result_unknown")
    effect.status, effect.remote_id = "confirmed_absent", None
    effect.result = {**effect.result, "recovered": True, "found": False}
    session.add(effect)
    work.pop("uncertain_effect", None)
    work.pop("uncertain_attempt_token", None)
    work.pop("remote_id", None)


def _known(
    session: Session, context: TenantContext, effect: ProviderEffect
) -> str | None:
    rows = session.exec(
        select(ProviderEffect.remote_id)
        .where(
            ProviderEffect.tenant_id == context.tenant_id,
            ProviderEffect.remote_scope_key == effect.remote_scope_key,
            ProviderEffect.step == "wy_receipt",
            col(ProviderEffect.result)["effect_id"].astext == str(effect.id),
        )
        .limit(2)
    ).all()
    identities = {_identity(value) for value in rows}
    if effect.remote_id:
        identities.add(_identity(effect.remote_id))
    if len(identities) > 1:
        raise core._error("provider_result_unknown")
    return next(iter(identities), None)


def _receipt(
    session: Session,
    context: TenantContext,
    effect_id: UUID,
    attempt: UUID,
    remote_id: str | None,
    *,
    accepted: bool,
) -> None:
    """Receipt first, without actor checks or item-claim ownership requirements."""
    effect = session.exec(
        select(ProviderEffect).where(
            ProviderEffect.tenant_id == context.tenant_id,
            ProviderEffect.id == effect_id,
            ProviderEffect.step == "create",
        )
    ).one()
    if effect.attempt_token != attempt:
        raise core._error("provider_state_invalid")
    if remote_id:
        remote_id = _identity(remote_id)
        # FK scope acquisition precedes touching the create effect: consistent
        # with item -> scope -> effect ordering of the normal workflow.
        fact = get_or_create_effect(
            session,
            tenant_id=context.tenant_id,
            scope_key=effect.remote_scope_key,
            step="wy_receipt",
            request_digest=core._digest([str(effect_id), remote_id]),
        )
        fact.status, fact.remote_id = "succeeded", remote_id
        fact.result = {
            "effect_id": str(effect_id),
            "attempt": str(attempt),
            "remote_id": remote_id,
        }
        session.add(fact)
        session.flush()
    if effect.status in {"sending", "result_unknown"}:
        effect.status = "result_unknown"
        effect.result = {**effect.result, "accepted": accepted}
        if remote_id and effect.remote_id in {None, remote_id}:
            effect.remote_id = remote_id
        session.add(effect)
    session.commit()


def _save_receipt(
    session: Session,
    context: TenantContext,
    effect_id: UUID,
    attempt: UUID,
    remote_id: str | None,
    *,
    accepted: bool,
) -> None:
    try:
        _receipt(session, context, effect_id, attempt, remote_id, accepted=accepted)
    except SQLAlchemyError:
        # The network result is already known; preserve it before HTTP cleanup.
        session.rollback()
        _receipt(session, context, effect_id, attempt, remote_id, accepted=accepted)


def _mark_uncertain(work: dict[str, Any], effect: ProviderEffect) -> None:
    if effect.attempt_token is None or effect.status not in {
        "sending",
        "result_unknown",
        "succeeded",
    }:
        raise core._error("provider_state_invalid")
    work["uncertain_effect"] = str(effect.id)
    work["uncertain_attempt_token"] = str(effect.attempt_token)
    work.pop("active_effect", None)


def _create(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
    client: WangyanClient,
) -> None:
    guard(session, context, item_id, token, work)
    effect = _creation(session, context, item_id, work, config)
    identity = effect.id
    if work.get("created_effect", str(identity)) != str(identity):
        raise core._error("provider_state_invalid")
    if effect.status == "failed":
        raise core._error(effect.result.get("error_code", "provider_rejected"))
    if effect.status not in {"pending", "confirmed_absent"}:
        if (
            effect.status not in {"sending", "result_unknown", "succeeded"}
            or effect.attempt_token is None
        ):
            raise core._error("provider_state_invalid")
        known = _known(session, context, effect)
        work["created_effect"] = str(identity)
        _mark_uncertain(work, effect)
        if known:
            work["remote_id"], work["stage"] = known, "verify"
        else:
            work["stage"] = "recover_create"
            _reset_scan(work)
        _checkpoint(session, context, item_id, token, work)
        return
    if not work.get("lookup_complete"):
        raise core._error("lookup_incomplete")
    # 重建前不得残留上次尝试的待核实标记或回执；出现即说明上次可能已落地。
    stale = effect.status == "confirmed_absent" and bool(work.get("uncertain_effect"))
    if stale or _known(session, context, effect) is not None:
        raise core._error("provider_result_unknown")
    payload = {
        "vid": work["drama"]["external_drama_id"],
        "drama_num": core._positive(config.get("episode", 1)),
    }
    effect.result = {"intent": payload}
    session.add(effect)
    session.flush()
    if not core.claim_effect(
        session, effect_id=identity, tenant_id=context.tenant_id, attempt_token=token
    ):
        raise core._error("provider_scope_busy")
    work["active_effect"] = work["created_effect"] = str(identity)
    _checkpoint(session, context, item_id, token, work)
    try:
        response = client.create_step("create", payload)
    except Exception as error:
        code = (
            error.code if isinstance(error, DomainError) else "provider_result_unknown"
        )
        if code == "provider_rejected":
            core._persist_effect(
                session,
                context,
                identity,
                token,
                status="failed",
                data={"error_code": code},
            )
            session.commit()
            raise core._error(code) from None
        _save_receipt(session, context, identity, token, None, accepted=False)
        work.update(
            uncertain_effect=str(identity),
            uncertain_attempt_token=str(token),
            stage="recover_create",
        )
        work.pop("active_effect", None)
        _reset_scan(work)
        raise core._error("provider_result_unknown") from None
    remote_id = response.get("remote_id")
    _save_receipt(session, context, identity, token, remote_id, accepted=True)
    work.update(uncertain_effect=str(identity), uncertain_attempt_token=str(token))
    work.pop("active_effect", None)
    if remote_id:
        work["remote_id"], work["stage"] = remote_id, "verify"
    else:
        work["stage"] = "recover_create"
        _reset_scan(work)
    _checkpoint(session, context, item_id, token, work)


def verified_data(
    data: dict[str, Any],
    work: dict[str, Any],
    config: dict[str, Any],
    application_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("config"), dict)
        or not isinstance(data.get("attribution"), dict)
    ):
        raise core._error("config_unverifiable")
    actual, attribution = data["config"], data["attribution"]
    remote_id = _identity(data.get("remote_id"))
    episode = core._positive(config.get("episode", 1))
    opaque = work["drama"]["external_drama_id"]
    if (
        remote_id != work.get("remote_id")
        or actual.get("vid") != opaque
        or core._positive(actual.get("drama_num")) != episode
        or attribution.get("app") != application_id
        or attribution.get("drama_id") != opaque
        or attribution.get("promote_platform") != "tiktok"
        or core._positive(attribution.get("id")) != int(remote_id)
        or core._positive(attribution.get("chapter_index")) != episode
    ):
        raise core._error("config_conflict")
    if not data.get("url") or data["url"] != actual.get("jump_url"):
        raise core._error("provider_result_unknown")
    protected = render_attribution(attribution, work["drama"]["title"])
    if data.get("protected_base") is not None and data["protected_base"] != protected:
        raise core._error("config_conflict")
    return {**data, "protected_base": protected}


def advance(
    session: Session,
    context: TenantContext,
    item_id: UUID,
    token: UUID,
    work: dict[str, Any],
    config: dict[str, Any],
    client: WangyanClient,
    connection_id: UUID,
    application_id: str,
) -> dict[str, Any] | None:
    if set(config) - {"episode"}:
        raise core._error("config_unverifiable")
    episode = core._positive(config.get("episode", 1))
    scope = core._digest(
        [
            str(context.tenant_id),
            str(connection_id),
            application_id,
            "wangyan",
            work["drama"]["external_drama_id"],
            episode,
        ]
    )
    if work.get("scope_key", scope) != scope:
        raise core._error("provider_state_invalid")
    work["scope_key"] = scope
    guard(session, context, item_id, token, work)
    if not claim_remote_scope(
        session, tenant_id=context.tenant_id, scope_key=scope, item_id=item_id
    ):
        session.commit()
        raise core._error("provider_scope_busy", retryable=True)
    session.commit()
    if work["stage"] == "create":
        _create(session, context, item_id, token, work, config, client)
        return None
    if work["stage"] == "verify":
        if work.get("created_effect") and not work.get("uncertain_effect"):
            # A local publication failure may follow an already verified effect.
            # Restore its original fence, never interpret it as ordinary history.
            _mark_uncertain(work, _creation(session, context, item_id, work, config))
        if work.get("uncertain_effect"):
            effect = _recovery_effect(session, context, item_id, work, config)
            known = _known(session, context, effect)
            if known and known != work.get("remote_id"):
                raise core._error("provider_result_unknown")
            session.commit()
        data = _read(
            session,
            context,
            item_id,
            token,
            work,
            lambda: client.read_link(_identity(work.get("remote_id"))),
        )
        data = verified_data(data, work, config, application_id)
        if work.get("uncertain_effect"):
            effect = _recovery_effect(session, context, item_id, work, config)
            effect.status, effect.remote_id = "succeeded", data["remote_id"]
            effect.result = {**effect.result, "verified": True}
            session.add(effect)
            work.pop("uncertain_effect", None)
            work.pop("uncertain_attempt_token", None)
        _checkpoint(session, context, item_id, token, work)
        return data
    if work["stage"] not in {"lookup", "recover_create"}:
        raise core._error("provider_state_invalid")
    recovering = work["stage"] == "recover_create"
    if recovering:
        effect = _recovery_effect(session, context, item_id, work, config)
        known = _known(session, context, effect)
        if known:
            work["remote_id"], work["stage"] = known, "verify"
            _checkpoint(session, context, item_id, token, work)
            return None
        session.commit()
    scan = _scan(work)
    if scan["done"]:
        _reset_scan(work)
        scan = _scan(work)
    try:
        data = _read(
            session,
            context,
            item_id,
            token,
            work,
            lambda: client.find_existing(
                work["drama"]["external_drama_id"], config, scan["cursor"]
            ),
        )
        rows, cursor = core._page(data)
        identities, total = data.get("observed_ids"), data.get("total")
        if (
            not isinstance(identities, list)
            or len(identities) > 20
            or type(total) is not int
            or not 0 <= total <= 200_000
            or len(rows) > len(identities)
            or scan["total"] is not None
            and scan["total"] != total
        ):
            raise core._error("lookup_incomplete")
        identities = [_identity(value) for value in identities]
        if len(set(identities)) != len(identities):
            raise core._error("lookup_incomplete")
        _observed(session, context, work, scan, identities)
        for row in rows:
            identity = _identity(row.get("remote_id"))
            if identity not in identities:
                raise core._error("lookup_incomplete")
            scan["matches"] = (
                min(2, scan["matches"] + 1) if recovering else scan["matches"] + 1
            )
            usable = bool(row.get("url"))
            if scan["candidate"] is None or (
                not recovering
                and (
                    (usable and not scan["usable"])
                    or usable == scan["usable"]
                    and int(identity) < int(scan["candidate"])
                )
            ):
                scan["candidate"], scan["usable"] = identity, usable
        scan.update(
            cursor=cursor,
            total=total,
            seen=scan["seen"] + len(identities),
            pages=scan["pages"] + 1,
        )
        if scan["pages"] > 10_000 or scan["seen"] > total:
            raise core._error("lookup_incomplete")
        if cursor is None:
            if scan["seen"] != total:
                raise core._error("lookup_incomplete")
            scan["done"] = True
            if recovering and scan["matches"] == 0:
                # 建链只发生在完整扫描证明无同参数链接之后，恢复扫描看到的同参数
                # 链接都只能来自这次建链；仍为 0 条说明本次未落地，可安全重建。
                _confirm_absent(session, context, work, config, item_id)
                work["lookup_complete"], work["stage"] = True, "create"
            elif recovering and scan["matches"] != 1:
                # 出现多条无法判断哪条属于本次建链，交人工核实，绝不重建。
                work["stage"] = "manual_review"
                _checkpoint(session, context, item_id, token, work)
                raise core._error("provider_result_unknown")
            elif scan["candidate"]:
                work["remote_id"], work["stage"] = scan["candidate"], "verify"
            else:
                work["lookup_complete"], work["stage"] = True, "create"
        _checkpoint(session, context, item_id, token, work)
    except DomainError as error:
        if recovering and error.code in {"lookup_incomplete", "config_unverifiable"}:
            session.rollback()
            _reset_scan(work)
            raise core._error("provider_result_unknown") from None
        raise
    return None
