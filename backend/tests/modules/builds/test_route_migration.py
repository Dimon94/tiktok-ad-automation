"""历史迁移只旁挂 attempt 身份；所有旧路由证据不足时明确阻断。"""

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, null, text
from sqlmodel import Session

from app.modules.accounts.models import TikTokConnection
from app.modules.builds.drafts import create_draft
from app.modules.builds.execution_models import (
    ExecutionStep,
    StepEvidence,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.models import BuildDraft, DraftInput
from app.modules.builds.preview_models import BuildPreview, BuildUnit, PreviewDrama
from app.modules.builds.routes import stable_attempt_id
from app.modules.providers.models import PromotionLink, ProviderDrama
from tests.migration_database import historical_database
from tests.modules.builds.test_drafts import account, create_intent
from tests.modules.conftest import create_context


def historical_rows(
    session, *, connections=1, context=None, current=False, preview_config=None
):
    """只播种旧 schema 已有事实，无冻结路由、无远端调用或业务 mock。"""
    context = context or create_context(session)
    intent = create_intent(session, context, historical=not current)
    if current:
        draft = create_draft(session, context=context, **intent)
    else:
        # 真正旧 schema 播种只使用当时已有列，不能用今日 ORM 新列伪造迁移前结构。
        row = BuildDraft(
            tenant_id=context.tenant_id,
            **{
                key: value
                for key, value in intent.items()
                if key not in {"drama_lines", "account_lines"}
            },
            created_by=context.actor_id,
            request_id=uuid4(),
            request_digest=hashlib.sha256(
                json.dumps(
                    intent,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
        )
        table = Table("build_draft", MetaData(), autoload_with=session.connection())
        session.execute(
            table.insert().values(
                **{
                    key: null()
                    if key in {"targeting_override", "structure_override"}
                    and value is None
                    else value
                    for key, value in row.model_dump().items()
                    if key in table.c
                }
            )
        )
        inputs = Table("draft_input", MetaData(), autoload_with=session.connection())
        for kind in ("drama", "account"):
            seen = {}
            for number, raw in enumerate(intent[kind + "_lines"], 1):
                value = raw.strip()
                duplicate = seen.get(value) if value else None
                item = DraftInput(
                    tenant_id=row.tenant_id,
                    draft_id=row.id,
                    kind=kind,
                    line_no=number,
                    raw_text=raw,
                    status="empty"
                    if not value
                    else "duplicate"
                    if duplicate
                    else "pending",
                    duplicate_of=duplicate,
                )
                session.execute(
                    inputs.insert().values(
                        **{
                            key: value
                            for key, value in item.model_dump().items()
                            if key in inputs.c
                        }
                    )
                )
                if value:
                    seen.setdefault(value, number)
        session.flush()
        draft = row.id
    if connections:
        account(session, context, historical=not current)
    for _ in range(max(0, connections - 1)):
        session.add(TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE"))
    preview = BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=intent["bc_id"],
        draft_id=draft,
        draft_revision=1,
        strategy_version_id=intent["strategy_version_id"],
        actor_id=context.actor_id,
        batch_short_id=uuid4().hex[:12],
        local_date="20260911",
        config=preview_config if preview_config is not None else {"historical": True},
        budget=Decimal("100"),
        target_roas=Decimal("1"),
        content_digest="a" * 64,
    )
    session.add(preview)
    session.flush()
    if not connections:
        return context, preview, None
    from app.modules.accounts.connection_models import BCDefaultRoute

    connection = session.get(BCDefaultRoute, (context.tenant_id, intent["bc_id"]))
    drama = ProviderDrama(
        tenant_id=context.tenant_id,
        connection_id=intent["provider_connection_id"],
        application_id=intent["application_id"],
        external_drama_id="old-drama",
        title="Old",
    )
    if current:
        session.add(drama)
    else:
        dramas = Table("provider_drama", MetaData(), autoload_with=session.connection())
        session.execute(
            dramas.insert().values(
                **{
                    key: value
                    for key, value in drama.model_dump().items()
                    if key in dramas.c
                }
            )
        )
    session.flush()
    link = PromotionLink(
        tenant_id=context.tenant_id,
        connection_id=drama.connection_id,
        application_id=drama.application_id,
        drama_id=drama.id,
        reuse_key="b" * 64,
    )
    if current:
        session.add(link)
    else:
        links = Table("promotion_link", MetaData(), autoload_with=session.connection())
        session.execute(
            links.insert().values(
                **{
                    key: value
                    for key, value in link.model_dump().items()
                    if key in links.c
                }
            )
        )
    session.flush()
    preview_drama = PreviewDrama(
        tenant_id=context.tenant_id,
        preview_id=preview.id,
        bc_id=preview.bc_id,
        drama_id=drama.id,
        link_id=link.id,
        title="Old",
        url="https://synthetic.invalid/old",
        protected_base="old",
    )
    if current:
        session.add(preview_drama)
    else:
        preview_dramas = Table(
            "preview_drama", MetaData(), autoload_with=session.connection()
        )
        session.execute(
            preview_dramas.insert().values(
                **{
                    key: value
                    for key, value in preview_drama.model_dump().items()
                    if key in preview_dramas.c
                }
            )
        )
    session.flush()
    unit = BuildUnit(
        tenant_id=context.tenant_id,
        preview_id=preview.id,
        bc_id=preview.bc_id,
        drama_id=drama.id,
        advertiser_id="account-A",
        connection_id=connection.connection_id,
        currency="USD",
        timezone="UTC",
        campaign_name="old-name",
        campaign_digest="c" * 64,
        scene_snapshot={},
        complete=True,
    )
    session.add(unit)
    session.flush()
    if current:
        from app.modules.accounts.routing import freeze_route
        from app.modules.builds.preview_models import PreviewContract
        from app.modules.builds.routes import save_preview_route

        # 当前 schema 的历史 V1 夹具也须具备迁移补齐的合同描述。
        session.add(
            PreviewContract(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=preview.bc_id,
                version=1,
                media_kind="VIDEO",
                legacy_material_layout="GROUP",
            )
        )
        session.flush()

        save_preview_route(
            session,
            context=context,
            preview_id=preview.id,
            route=freeze_route(session, context=context, bc_id=preview.bc_id),
        )
    preview.status = "FROZEN"
    submission = Submission(
        tenant_id=context.tenant_id,
        bc_id=preview.bc_id,
        preview_id=preview.id,
        draft_id=draft,
        actor_id=context.actor_id,
        ordinal=1,
    )
    session.add(submission)
    session.flush()
    session.add(
        SubmissionUnit(
            tenant_id=context.tenant_id,
            submission_id=submission.id,
            unit_id=unit.id,
            preview_id=preview.id,
            bc_id=preview.bc_id,
            disposition="INCLUDED",
        )
    )
    session.flush()
    step = ExecutionStep(
        tenant_id=context.tenant_id,
        submission_id=submission.id,
        preview_id=preview.id,
        bc_id=preview.bc_id,
        unit_id=unit.id,
        kind="CAMPAIGN",
        step_key="old",
        attempt=1 if current else 3,
        status="PENDING" if current else "UNKNOWN",
        phase="IDLE" if current else "REQUEST_ARMED",
        remote_id=None if current else "already-created",
        request_body=None
        if current
        else {"budget": "100.010000000000000001", "name": "原始请求"},
        request_body_digest=None if current else "d" * 64,
        resolved={"page": 2, "ids": ["kept"]},
    )
    if current:
        from app.modules.builds.routes import save_attempt_context

        session.add(step)
        save_attempt_context(session, step=step)
        session.flush()
        return context, preview, step
    # Reflect the historical table: the new current attempt_id column does not exist yet.
    table = Table("execution_step", MetaData(), autoload_with=session.connection())
    session.execute(table.insert().values(**step.model_dump(exclude={"attempt_id"})))
    evidence = Table("step_evidence", MetaData(), autoload_with=session.connection())
    for count in (1, 2, 3):
        row = StepEvidence(
            tenant_id=context.tenant_id,
            submission_id=submission.id,
            step_id=step.id,
            attempt=count,
            conclusion="LATE_CREATED",
            request_id=f"original-{count}",
            summary={"remote_id": f"received-{count}", "body_digest": "d" * 64},
        )
        session.execute(evidence.insert().values(**row.model_dump()))
    return context, preview, step


def snapshot(connection):
    return {
        table: connection.execute(text(query)).all()
        for table, query in {
            "step": "SELECT id,tenant_id,attempt,request_body::text,request_body_digest,remote_id,resolved::text FROM execution_step ORDER BY id",
            "evidence": "SELECT row_to_json(e)::text FROM step_evidence e ORDER BY id",
            "preview": "SELECT id,config::text,content_digest,batch_short_id FROM build_preview ORDER BY id",
        }.items()
    }


def test_empty_history_creates_no_fabricated_context(monkeypatch):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        command.upgrade(config, "mcp_build_routes")
        with engine.connect() as connection:
            for table in ("build_route_context", "build_attempt_context"):
                assert (
                    connection.execute(
                        text(f"SELECT count(*) FROM {table}")
                    ).scalar_one()
                    == 0
                )


@pytest.mark.parametrize("connections", [0, 1, 2])
def test_old_single_multi_or_missing_connection_never_proves_historical_authority(
    monkeypatch, connections
):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            context, preview, step = historical_rows(session, connections=connections)
            tenant_id, preview_id = context.tenant_id, preview.id
            step_id = step.id if step else None
        with engine.connect() as connection:
            before = snapshot(connection)
        command.upgrade(config, "mcp_build_routes")
        with engine.connect() as connection:
            assert snapshot(connection) == before
            assert (
                connection.execute(
                    text("SELECT count(*) FROM build_route_context")
                ).scalar_one()
                == 0
            )
            if step_id is None:
                assert (
                    connection.execute(
                        text("SELECT status FROM build_preview WHERE id=:id"),
                        {"id": preview_id},
                    ).scalar_one()
                    == "OBSOLETE"
                )
            else:
                assert (
                    connection.execute(
                        text("SELECT error_code FROM build_submission")
                    ).scalar_one()
                    == "legacy_route_unverifiable"
                )
                rows = connection.execute(
                    text(
                        "SELECT attempt,attempt_id FROM build_attempt_context WHERE step_id=:id ORDER BY attempt"
                    ),
                    {"id": step_id},
                ).all()
                assert rows == [
                    (
                        n,
                        stable_attempt_id(
                            tenant_id=tenant_id, step_id=step_id, attempt=n
                        ),
                    )
                    for n in (1, 2, 3)
                ]
                assert (
                    connection.execute(
                        text("SELECT attempt_id FROM execution_step WHERE id=:id"),
                        {"id": step_id},
                    ).scalar_one()
                    == rows[-1].attempt_id
                )


def test_same_bc_in_two_tenants_keeps_attempts_scoped_without_guessing_routes(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            first, _, first_step = historical_rows(session)
            second, _, second_step = historical_rows(session)
        command.upgrade(config, "mcp_build_routes")
        with engine.connect() as connection:
            pairs = connection.execute(
                text(
                    "SELECT tenant_id,step_id FROM build_attempt_context WHERE attempt=3"
                )
            ).all()
            assert set(pairs) == {
                (first.tenant_id, first_step.id),
                (second.tenant_id, second_step.id),
            }
            assert (
                connection.execute(
                    text("SELECT count(*) FROM build_route_context")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM build_submission WHERE error_code='legacy_route_unverifiable'"
                    )
                ).scalar_one()
                == 2
            )


def test_legacy_submission_cannot_request_recovery_even_with_current_default(
    monkeypatch,
):
    from app.core.errors import DomainError
    from app.modules.builds.recovery import request_recovery
    from app.modules.builds.routes import load_preview_route

    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            context, preview, step = historical_rows(session)
            preview_id, submission_id = preview.id, step.submission_id
        # 当前应用读取使用当前 schema；旧事实仍必须在完整升级后明确阻断。
        command.upgrade(config, "head")
        with Session(engine) as session:
            with pytest.raises(
                DomainError,
                check=lambda error: error.code == "legacy_route_unverifiable",
            ):
                load_preview_route(session, context=context, preview_id=preview_id)
            with pytest.raises(
                DomainError,
                check=lambda error: error.code == "legacy_route_unverifiable",
            ):
                request_recovery(
                    session,
                    context=context,
                    submission_id=submission_id,
                    request_id=uuid4(),
                    kind="RECONCILE",
                )
