"""一万真实PG组合的准入/投递上界，不冒充一万次远端广告创建。"""

import json
from time import perf_counter
from uuid import uuid4

from sqlalchemy import insert, text
from sqlmodel import Session, col, select

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.builds import execution_window
from app.modules.builds.dependency_waits import wake_material_dependencies
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.preview_models import (
    BuildPreview,
    BuildUnit,
    PreviewAdCreativeUnit,
    PreviewContract,
    PreviewCreativeUnit,
    PreviewCreativeUnitItem,
    PreviewDrama,
    PreviewDramaGroup,
    PreviewGroupCreativeUnit,
)
from app.modules.builds.routes import load_preview_route, save_preview_route
from app.modules.materials.cover_candidates import candidate_query
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from tests.modules.builds.test_execution import executable as executable


def test_ten_thousand_units_only_dispatch_active_window(executable, monkeypatch):
    database, context, _ = executable
    monkeypatch.setattr(execution_window, "MAX_ACTIVE_UNITS", 10)
    with Session(database) as db, db.begin():
        unit = db.exec(select(BuildUnit)).one()
        submitted = db.exec(select(SubmissionUnit)).one()
        account = db.get(AdvertiserAccount, (context.tenant_id, unit.advertiser_id))
        access = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id,
                BCAccountAccess.advertiser_id == unit.advertiser_id,
            )
        ).one()
        steps = db.exec(
            select(ExecutionStep).where(ExecutionStep.kind == "MATERIAL")
        ).all()
        asset = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == steps[0].material_id
            )
        ).one()
        route = load_preview_route(db, context=context, preview_id=unit.preview_id)
        # 容量数据在新的BUILDING快照内构造后冻结，不关闭不可变触发器，
        # 也不向既有冻结预览补写伪造组合。
        preview_id, submission_id = uuid4(), uuid4()
        original_preview = db.get(BuildPreview, unit.preview_id)
        preview = BuildPreview(
            **{
                **original_preview.model_dump(),
                "id": preview_id,
                "draft_revision": original_preview.draft_revision + 1,
                "status": "BUILDING",
                "batch_short_id": uuid4().hex,
                "dispatch_id": None,
            }
        )
        db.add(preview)
        db.flush()
        drama = db.exec(
            select(PreviewDrama).where(PreviewDrama.preview_id == unit.preview_id)
        ).one()
        db.add(PreviewDrama(**{**drama.model_dump(), "preview_id": preview_id}))
        db.flush()
        # 与真实 V2 预览一致：冻结单元、图片/视频成员和分组/广告映射，
        # 不再向已退休的 legacy material 表补写数据。
        for model in (
            PreviewContract,
            PreviewCreativeUnit,
            PreviewCreativeUnitItem,
            PreviewDramaGroup,
            PreviewGroupCreativeUnit,
            PreviewAdCreativeUnit,
        ):
            originals = db.exec(
                select(model).where(model.preview_id == unit.preview_id)
            ).all()
            assert originals
            db.execute(
                insert(model),
                [{**item.model_dump(), "preview_id": preview_id} for item in originals],
            )
        save_preview_route(db, context=context, preview_id=preview_id, route=route)
        original_submission = db.get(Submission, submitted.submission_id)
        db.add(
            Submission(
                **{
                    **original_submission.model_dump(),
                    "id": submission_id,
                    "preview_id": preview_id,
                    "ordinal": original_submission.ordinal + 1,
                    "dispatch_id": None,
                }
            )
        )
        db.flush()
        models = {
            model: []
            for model in (
                AdvertiserAccount,
                BCAccountAccess,
                BuildUnit,
                SubmissionUnit,
                AccountMaterial,
                PendingDispatch,
                MaterialCoverJob,
                ExecutionStep,
            )
        }
        for index in range(10000):
            identity = uuid4()
            asset_id, cover_id, cover_dispatch_id = uuid4(), uuid4(), uuid4()
            advertiser = f"capacity-{index}"
            models[AdvertiserAccount].append(
                {**account.model_dump(), "advertiser_id": advertiser}
            )
            models[BCAccountAccess].append(
                {**access.model_dump(), "advertiser_id": advertiser}
            )
            models[BuildUnit].append(
                {
                    **unit.model_dump(),
                    "id": identity,
                    "preview_id": preview_id,
                    "advertiser_id": advertiser,
                    "campaign_name": advertiser,
                    "campaign_digest": f"{index:064x}",
                }
            )
            models[SubmissionUnit].append(
                {
                    **submitted.model_dump(),
                    "unit_id": identity,
                    "dispatch_id": None,
                    "preview_id": preview_id,
                    "submission_id": submission_id,
                }
            )
            models[AccountMaterial].append(
                {**asset.model_dump(), "id": asset_id, "advertiser_id": advertiser}
            )
            # 候选必须具备可领取的持久投递；容量夹具也包含真实消息身份，
            # 不能用一万条缺失dispatch的等待任务替代已排队候选。
            models[PendingDispatch].append(
                PendingDispatch(
                    id=cover_dispatch_id,
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    task_name="materials.prepare_cover",
                    task_key=f"cover:{cover_id}:1",
                    payload={"job_id": str(cover_id), "revision": 1},
                ).model_dump()
            )
            models[MaterialCoverJob].append(
                MaterialCoverJob(
                    id=cover_id,
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    bc_id=unit.bc_id,
                    material_id=steps[0].material_id,
                    asset_id=asset_id,
                    advertiser_id=advertiser,
                    connection_id=unit.connection_id,
                    frozen_route=route.model_dump(mode="json"),
                    video_id=asset.video_id,
                    remote_name=f"capacity-{index}.jpg",
                    dispatch_id=cover_dispatch_id,
                    revision=1,
                ).model_dump()
            )
            models[ExecutionStep].append(
                {
                    **steps[0].model_dump(),
                    "id": uuid4(),
                    "unit_id": identity,
                    "preview_id": preview_id,
                    "submission_id": submission_id,
                    "step_key": f"capacity:{identity}",
                    "dispatch_id": None,
                    "error_code": "execution_window_wait",
                    "cover_job_id": cover_id,
                }
            )
        for model, rows in models.items():
            db.execute(insert(model), rows)
        preview.status = "FROZEN"
        for table in (
            "build_unit",
            "submission_unit",
            "execution_step",
            "material_cover_job",
        ):
            db.execute(text("ANALYZE " + table))
    with Session(database) as db:
        started = perf_counter()
        rows = db.exec(
            execution_window.window_units().where(
                SubmissionUnit.submission_id == submission_id
            )
        ).all()
        elapsed = perf_counter() - started
        assert len(rows) == 10 and elapsed < 3
        active = {row[2] for row in rows}
        expected = len(
            db.exec(
                select(ExecutionStep.id).where(
                    col(ExecutionStep.unit_id).in_(active),
                    ExecutionStep.kind == "MATERIAL",
                )
            ).all()
        )
        before = len(db.exec(select(PendingDispatch.id)).all())
        anchor = db.exec(
            select(MaterialCoverJob)
            .join(ExecutionStep, ExecutionStep.cover_job_id == MaterialCoverJob.id)
            .where(col(ExecutionStep.unit_id).in_(active))
        ).first()
        db.execute(text("SET LOCAL statement_timeout='4000ms'"))
        started = perf_counter()
        candidates = db.exec(candidate_query(anchor)).all()
        candidate_seconds = perf_counter() - started
        assert len(candidates) == 10 and candidate_seconds < 3
    scheduled = wake_material_dependencies(database_engine=database)
    assert scheduled == expected and scheduled <= 20
    assert wake_material_dependencies(database_engine=database) == 0
    with Session(database) as db:
        assert len(db.exec(select(PendingDispatch.id)).all()) - before == scheduled
        assert not db.exec(
            select(ExecutionStep.id).where(
                ~col(ExecutionStep.unit_id).in_(active),
                ExecutionStep.kind == "MATERIAL",
                col(ExecutionStep.dispatch_id).is_not(None),
            )
        ).first()
    print(  # noqa: T201 - 明确输出容量测量，非业务吞吐承诺。
        json.dumps(
            {
                "units": 10000,
                "active_units": 10,
                "window_seconds": round(elapsed, 4),
                "scheduled": scheduled,
                "candidate_seconds": round(candidate_seconds, 4),
            }
        ),
        flush=True,
    )
