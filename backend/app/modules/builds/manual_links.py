"""手动输入只建立本地资料，不发起版权方业务请求。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from typing import cast as typing_cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import delete, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.providers.repository import get_connection
from app.modules.tenants.permissions import require_tenant

from .models import BuildDraft, DraftDrama, DraftInput
from .schemas import ManualLinkInput


def resolve_provider(
    session: Session,
    context: TenantContext,
    connection_id: UUID | None,
    application_id: str | None,
    name: str | None,
) -> tuple[UUID, str]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    if name is None:
        if not connection_id or not application_id:
            raise DomainError(
                "draft_input_invalid", "请选择版权方及推广应用，或填写其他版权方名称"
            )
        return connection_id, application_id
    name = name.strip()
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise DomainError("draft_input_invalid", "请填写有效的版权方名称")
    if connection_id or application_id:
        raise DomainError("draft_input_invalid", "其他版权方不能同时指定自动取链连接")
    # 稳定租户内标识及事务锁确保并发新增同名资料不会产生多个范围。
    identity = uuid5(context.tenant_id, f"manual-provider:{name}")
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(identity)},
    )
    provider = session.get(ProviderConnection, identity)
    if provider is None:
        provider = ProviderConnection(
            id=identity,
            tenant_id=context.tenant_id,
            kind="other",
            display_name=name,
            status="local",
            encrypted_credentials=None,
        )
        session.add(provider)
        session.flush()
        session.add(
            ProviderApplication(
                tenant_id=context.tenant_id,
                connection_id=identity,
                external_id="manual",
                name="手动录入",
                channel_config={"source": "local"},
            )
        )
        session.flush()
    if (
        provider.tenant_id != context.tenant_id
        or provider.kind != "other"
        or provider.status != "local"
    ):
        raise DomainError("draft_input_invalid", "版权方资料不可用")
    return identity, "manual"


def validate_manual_links(
    values: list[dict[str, Any]], lines: list[str], kind: str
) -> dict[int, dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 1000:
        raise DomainError("manual_link_invalid", "手动链接最多 1000 行")
    result: dict[int, dict[str, Any]] = {}
    for raw in values:
        try:
            value = ManualLinkInput.model_validate(raw)
            if any(ord(c) < 32 for c in value.material_identifier):
                raise ValueError
            value.material_identifier = value.material_identifier.strip()
            value.external_drama_id = value.external_drama_id.strip()
            value.protected_base = value.protected_base.strip()
            parts = urlsplit(value.url)
            path = parts.path.rstrip("/").split("/")
            mini_values = parse_qs(parts.query, keep_blank_values=True).get("minis_id")
            if mini_values is not None:
                from .mini_targets import explicit_mini_id

                if not explicit_mini_id(value.url):
                    raise ValueError
            if (
                value.line_no > len(lines)
                or not lines[value.line_no - 1].strip()
                or value.line_no in result
                or any(c.isspace() or ord(c) < 32 for c in value.url)
                or (
                    value.url
                    and (
                        parts.scheme != "https"
                        or parts.hostname not in {"www.tiktok.com", "tiktok.com"}
                        or parts.username
                        or parts.password
                        or parts.port not in {None, 443}
                        or len(path) != 3
                        or path[1] != "minis"
                        or not path[2]
                    )
                )
                or any(
                    ord(c) < 32
                    for c in value.external_drama_id
                    + value.protected_base
                    + value.material_identifier
                )
            ):
                raise ValueError
        except ValidationError, ValueError, TypeError:
            raise DomainError(
                "manual_link_invalid",
                "请检查剧目行号，并填写完整的 HTTPS TikTok Minis 推广链接",
            ) from None
        if kind == "jiashu" and value.url and value.external_drama_id:
            # 嘉书链接的 vid 就是剧目编号；只校验明确提供的编号，不推断其他版权方。
            vids = parse_qs(parts.query, keep_blank_values=True).get("vid")
            if vids is not None and vids != [value.external_drama_id]:
                raise DomainError(
                    "manual_link_drama_mismatch",
                    "推广链接的 vid 与剧目编号不一致，请核对嘉书链接",
                )
        if kind == "wangyan" and value.url and not value.protected_base.strip():
            raise DomainError(
                "manual_attribution_required",
                "网眼手动链接需同时填写版权方提供的归因名称",
            )
        result[value.line_no] = value.model_dump()
    return result


def materialize(
    session: Session, context: TenantContext, draft: BuildDraft, row: DraftInput
) -> None:

    value = row.manual_link
    if not value.get("url"):
        row.status, row.reason_code = "failed", "manual_link_required"
        row.provider_input_id = None
        row.candidates = []
        return
    # 缺少外部 ID 时，本地编号明确带 LOCAL 标记；不凭剧名合并不同链接。
    identity_basis = json.dumps(
        [
            str(draft.provider_connection_id),
            draft.application_id,
            row.raw_text.strip(),
            value["url"],
        ],
        ensure_ascii=False,
    )
    external = (
        value.get("external_drama_id", "").strip()
        or f"LOCAL-{sha256(identity_basis.encode()).hexdigest()[:16]}"
    )
    # 同版权方/应用的本地准备串行，避免两批剧目顺序相反时互持多把锁；锁内无远端请求。
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": f"manual-scope:{context.tenant_id}:{draft.provider_connection_id}:{draft.application_id}"
        },
    )
    # 手动表单填写展示编号时复用同范围的剧目，不能覆盖其取链接口标识。
    query = select(ProviderDrama).where(
        col(ProviderDrama.tenant_id) == context.tenant_id,
        col(ProviderDrama.connection_id) == draft.provider_connection_id,
        col(ProviderDrama.application_id) == draft.application_id,
        or_(
            col(ProviderDrama.external_drama_id) == external,
            col(ProviderDrama.display_drama_id) == external,
        ),
    )

    def matching_drama() -> ProviderDrama | None:
        matches = session.exec(query.limit(2)).all()
        if len(matches) > 1:
            raise DomainError(
                "manual_link_invalid", "版权方剧目 ID 对应多条记录，请先核对剧目"
            )
        return matches[0] if matches else None

    drama = matching_drama()
    if drama is None:
        candidate = ProviderDrama(
            tenant_id=context.tenant_id,
            connection_id=typing_cast(UUID, draft.provider_connection_id),
            application_id=typing_cast(str, draft.application_id),
            external_drama_id=external,
            display_drama_id=value.get("external_drama_id", "").strip() or None,
            title=row.raw_text.strip(),
        )
        # 自动发现可能刚以长 ID 保存同一数字编号；撤销本次临时插入后复用它。
        with session.begin_nested() as insertion:
            session.exec(
                insert(ProviderDrama)
                .values(**candidate.model_dump())
                .on_conflict_do_nothing(constraint="uq_provider_drama_external")
            )
            try:
                drama = matching_drama()
            except DomainError:
                insertion.rollback()
        if drama is None:
            drama = matching_drama()
        if drama is None:
            raise DomainError("manual_link_invalid", "剧目记录已变化，请重试")
    digest = sha256(
        json.dumps(
            ["manual", str(context.tenant_id), str(row.id), str(drama.id), value],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    link = session.exec(
        select(PromotionLink).where(
            col(PromotionLink.tenant_id) == context.tenant_id,
            col(PromotionLink.reuse_key) == digest,
        )
    ).one_or_none()
    if link is None:
        link = PromotionLink(
            tenant_id=context.tenant_id,
            drama_id=drama.id,
            connection_id=typing_cast(UUID, draft.provider_connection_id),
            application_id=typing_cast(str, draft.application_id),
            reuse_key=digest,
            source="manual",
            url=value["url"],
            protected_base=value.get("protected_base", ""),
            status="ready",
            verified_at=datetime.now(UTC),
            attribution={
                "source": "manual",
                "recorded_by": str(context.actor_id),
                "external_drama_id": value.get("external_drama_id", ""),
                "verification": "local_format_only",
            },
        )
        session.add(link)
        session.flush()
    previous = session.get(DraftDrama, (context.tenant_id, draft.id, drama.id))
    if previous and previous.first_line != row.line_no:
        if previous.link_id != link.id:
            raise DomainError(
                "manual_link_conflict", "同一剧目 ID 对应不同输入，请合并为一行"
            )
        row.status, row.duplicate_of = "duplicate", previous.first_line
        return
    if previous is None:
        session.add(
            DraftDrama(
                tenant_id=context.tenant_id,
                draft_id=draft.id,
                bc_id=draft.bc_id,
                drama_id=drama.id,
                link_id=link.id,
                title=row.raw_text.strip(),
                first_line=row.line_no,
            )
        )
    row.drama_id, row.status, row.reason_code = drama.id, "ready", None
    row.provider_input_id, row.candidates = None, []


def save_manual_link(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    input_id: UUID,
    expected_revision: int,
    request_id: UUID,
    link: dict[str, Any],
) -> int:
    from .drafts import _bump_revision, get_draft
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != expected_revision:
            raise DomainError("draft_revision_conflict", "草稿已更新，请刷新后补充")
        if draft.status == "PREPARING":
            raise DomainError("draft_not_ready", "请等待当前准备完成后补充链接")
        row = session.exec(
            select(DraftInput).where(
                col(DraftInput.tenant_id) == context.tenant_id,
                col(DraftInput.draft_id) == draft_id,
                col(DraftInput.id) == input_id,
                col(DraftInput.kind) == "drama",
            )
        ).one_or_none()
        if row is None or not row.raw_text.strip() or row.status == "duplicate":
            raise DomainError("draft_not_found", "当前草稿的有效剧目输入不存在")
        if link.get("line_no") != row.line_no:
            raise DomainError("manual_link_invalid", "链接与剧目行号不一致")

        provider = get_connection(
            session,
            context=context,
            connection_id=typing_cast(UUID, draft.provider_connection_id),
        )
        lines = [""] * row.line_no
        lines[-1] = row.raw_text
        value = validate_manual_links([link], lines, provider.kind)[row.line_no]
        if not value["url"]:
            raise DomainError("manual_link_required", "请补充推广链接")
        # 第二步补链只修改链接信息，保留首次录入的素材匹配标识。
        value["material_identifier"] = row.manual_link.get("material_identifier", "")
        if row.manual_link == value:
            return draft.revision
        if row.drama_id:
            from .video_units import delete_video_units

            delete_video_units(session, draft, drama_id=row.drama_id)
            for model in (DraftDrama,):
                SASession.execute(
                    session,
                    delete(model).where(
                        col(model.tenant_id) == context.tenant_id,
                        col(model.draft_id) == draft_id,
                        col(model.drama_id) == row.drama_id,
                    ),
                )
        row.manual_link = value
        row.drama_id, row.provider_input_id = None, None
        row.candidates, row.status, row.reason_code = [], "pending", None
        draft.status = "DRAFT"
        session.add(row)
        return _bump_revision(session, draft, expected_revision)

    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=request_id,
        kind="manual_link",
        intent={
            "input_id": str(input_id),
            "expected_revision": expected_revision,
            "link": link,
        },
        operation=apply,
    )
