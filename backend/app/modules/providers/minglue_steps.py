"""明略持久取链步骤：每轮一个业务请求，写入复用共享 scope/effect 台账。"""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext

from .adapters.contract import ProviderClient, external_id
from .adapters.minglue import MinglueClient
from .link_steps import (
    _digest,
    _effect_request,
    _error,
    _finish_unit,
    _load,
    _positive,
    _publish,
    _read_effect,
    _recover_effect,
    _resolved_drama,
)
from .repository import claim_remote_scope


def verify_data(data: dict[str, Any], work: dict[str, Any]) -> None:
    """已复用与新建链接都必须由详情证明应用、剧目及落地剧集一致。"""
    expected = work.get("minglue_config")
    actual = data.get("config")
    if (
        not isinstance(expected, dict)
        or not isinstance(actual, dict)
        or data.get("remote_id") != work["channel"]
        or not data.get("url")
        or data.get("protected_base") is None
        or not isinstance(data.get("attribution"), dict)
    ):
        raise _error("config_unverifiable")
    for key in ("partner_id", "app_id", "content_id", "section_id"):
        if actual.get(key) != expected.get(key):
            raise _error("config_conflict")


def run_item(
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
    if not isinstance(client, MinglueClient):
        raise _error("provider_state_invalid")
    stage = work["stage"]
    if stage == "search":
        page = client.search(raw_input.strip(), str(work["search_page"]))
        candidates = {row["external_drama_id"]: row for row in work["candidates"]}
        for candidate in page.items:
            row = candidate.model_dump()
            previous = candidates.get(row["external_drama_id"])
            if previous is not None and previous != row:
                raise _error("provider_schema_unsupported")
            candidates[row["external_drama_id"]] = row
        work["candidates"] = list(candidates.values())
        if not page.complete:
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
            work["stage"] = "ml_visibility"
        _finish_unit(session, context, item_id, token, work)
        return

    drama_id = work["drama"]["external_drama_id"]
    if stage == "ml_visibility":
        client.check_visibility(drama_id)
        work["stage"] = "ml_sections"
        work["section_cursor"] = None
        work["section_found"] = None
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "ml_sections":
        rows, cursor = client.section_page(drama_id, work.get("section_cursor"))
        wanted = _positive(config.get("episode", 1))
        for row in rows:
            if client.section_number(row) == wanted:
                section_id = external_id(row.get("id"))
                if work.get("section_found") not in (None, section_id):
                    raise _error("provider_schema_unsupported")
                work["section_found"] = section_id
        if cursor:
            work["section_cursor"] = cursor
        else:
            if work.get("section_found") is None:
                raise _error("link_episode_out_of_range")
            work["minglue_config"] = {
                "episode": wanted,
                "section_id": work["section_found"],
                "content_id": drama_id,
                "app_id": application_id,
                "partner_id": client.channel_config["partner_id"],
                "content_type": client.channel_config["content_type"],
                "name": f"{work['drama']['title']}_{item_id.hex[:12]}",
            }
            work["stage"] = "lookup"
            work["lookup_cursor"], work["lookup_found"] = None, None
        _finish_unit(session, context, item_id, token, work)
        return

    effective = work.get("minglue_config")
    if not isinstance(effective, dict):
        raise _error("provider_state_invalid")
    # scope 与后台真实复用维度一致，不能把名称/日期放进锁键。
    work["scope_key"] = _digest(
        [
            str(context.tenant_id),
            str(connection_id),
            "minglue",
            application_id,
            effective["partner_id"],
            drama_id,
            effective["section_id"],
        ]
    )
    work.setdefault(
        "channel", "ml-" + _digest([drama_id, application_id, effective["section_id"]])
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
        page = client.lookup_link(drama_id, effective, work.get("lookup_cursor"))
        for row in page.items:
            # 新建超时只恢复本任务名称的记录，不能把历史同参数链接当成新写入证据。
            if stage == "recover_create" and row.get("name") != effective["name"]:
                continue
            work["lookup_found"] = external_id(row.get("promotionId", row.get("id")))
        if not page.complete:
            work["lookup_cursor"] = page.next_cursor
        else:
            found = work.get("lookup_found")
            if stage == "recover_create" and not found:
                # 接口无幂等键，也没有强一致的不存在证明，保留未知写入，禁止盲目重放。
                work["lookup_cursor"] = None
                raise _error("provider_result_unknown")
            if found:
                work["channel"] = found
            if stage == "recover_create":
                _recover_effect(session, context, work, found=True)
            work["lookup_complete"] = True
            work["stage"] = "read_before" if found else "create"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage == "create":
        if not work.get("lookup_complete"):
            raise _error("lookup_incomplete")
        # 在发出写入前清除旧分页状态，进程崩溃后从第一页核查。
        work["lookup_cursor"], work["lookup_found"] = None, None
        response = _effect_request(
            session,
            context,
            item_id,
            token,
            work,
            "create",
            {"vid": drama_id, "config": effective},
            client,
        )
        work["channel"] = external_id(response.get("remote_id"))
        work["stage"] = "verify"
        _finish_unit(session, context, item_id, token, work)
        return

    if stage in {"read_before", "verify"}:
        data = _read_effect(session, context, item_id, token, work, client)
        verify_data(data, work)
        _publish(session, context, item_id, token, work, data)
        return
    raise _error("provider_state_invalid")
