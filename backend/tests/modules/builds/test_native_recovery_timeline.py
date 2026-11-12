"""公开恢复入口 → 真实 PG/Redis → SDK GET；只推进到期时钟，不伪造业务状态。"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds import execution, reconciliation, recovery, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from tests.modules.builds.test_native_readback_timeline import observed_rows


def recover_unknown(
    engine, context, redis, monkeypatch, submission_id, creates, scenario
):
    kind = {"unknown_group": "ADGROUP", "unknown_ad": "AD"}.get(scenario, "CAMPAIGN")
    index = {"CAMPAIGN": 2, "ADGROUP": 3, "AD": 4}[kind]
    rid = f"remote-{index}"
    rows = [observed_rows(creates)[rid]]
    if scenario == "unknown_ambiguous":
        other = deepcopy(rows[0])
        other["campaign_id"] = "second"
        rows.append(other)
    with Session(engine) as db, db.begin():
        source = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == submission_id,
                ExecutionStep.kind == kind,
                ExecutionStep.status == "UNKNOWN",
            )
        ).one()
        assert source.status == "UNKNOWN" and source.remote_id is None
        source.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.add(source)
        sid = source.id
        original_body, digest = source.request_body, source.request_body_digest
        from app.modules.builds.preview_models import BuildUnit

        connection_id = db.get(BuildUnit, source.unit_id).connection_id
    if scenario in {"unknown_cancel", "unknown_late"}:
        with Session(engine) as db, db.begin():
            submissions.cancel_submission(
                db, context=context, submission_id=submission_id
            )
    with Session(engine) as db, db.begin():
        receipt = recovery.request_recovery(
            db,
            context=context,
            submission_id=submission_id,
            request_id=uuid4(),
            kind="RECONCILE",
        )
    recovery.process_recovery(
        database_engine=engine,
        context=context,
        payload={"recovery_id": str(receipt.recovery_id), "revision": 0},
    )
    reads = []

    def request(_pool, method, url, **_kwargs):
        assert method == "GET"
        reads.append(url)
        if scenario == "unknown_revoke":
            from app.modules.accounts.models import TikTokConnection

            with Session(engine) as db, db.begin():
                conn = db.get(TikTokConnection, connection_id)
                conn.status = "DISABLED"
                db.add(conn)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "request_id": "recovered",
                    "data": {
                        "list": rows,
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_page": 1,
                            "total_number": len(rows),
                        },
                    },
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    monkeypatch.setattr(
        reconciliation,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(45, 40), called_directly=False, is_eager=False
            )
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    with Session(engine) as db:
        source = db.get(ExecutionStep, sid)
        revision = source.dispatch_revision
    outcome = reconciliation.process_reconciliation(
        database_engine=engine,
        redis_client=redis,
        context=context,
        step_id=sid,
        revision=revision,
    )
    assert len(reads) == 1
    assert outcome.state == (
        "UNKNOWN"
        if scenario in {"unknown_ambiguous", "unknown_revoke"}
        else "SUCCEEDED"
    )
    with Session(engine) as db:
        source = db.get(ExecutionStep, sid)
        assert (
            source.request_body == original_body
            and source.request_body_digest == digest
        )
        assert source.remote_id == (
            None if scenario in {"unknown_ambiguous", "unknown_revoke"} else rid
        )
        evidence = db.exec(
            select(StepEvidence).where(StepEvidence.step_id == sid)
        ).all()
        assert any(
            x.conclusion
            == ("LATE_CREATED" if scenario == "unknown_late" else "RESULT_UNKNOWN")
            for x in evidence
        )
        children = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == submission_id,
                ExecutionStep.kind == "ADGROUP",
            )
        ).all()
    # 取消后的恢复只能保存事实；旧工作消息不能继续创建。
    if scenario in {"unknown_cancel", "unknown_late"}:
        for child in children:
            assert (
                execution.process_step(
                    database_engine=engine,
                    redis_client=redis,
                    context=context,
                    step_id=child.id,
                    revision=child.dispatch_revision,
                )
                == "FAILED"
            )
        assert len(reads) == 1

    if scenario == "unknown_revoke":
        with Session(engine) as db, db.begin():
            submissions.cancel_submission(
                db, context=context, submission_id=submission_id
            )
        assert len(reads) == 1

    if scenario in {"unknown_unique", "unknown_group"}:
        # 父级认领完成后，才允许原计划下一层发送；不重建已认领父级。
        next_kind = "ADGROUP" if kind == "CAMPAIGN" else "AD"
        with Session(engine) as db:
            child = db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == submission_id,
                    ExecutionStep.kind == next_kind,
                )
            ).first()
        sent = []

        def resume(_pool, method, url, **kwargs):
            assert method == "POST"
            payload = json.loads(kwargs["body"])
            assert (
                payload["campaign_id" if next_kind == "ADGROUP" else "adgroup_id"]
                == rid
            )
            sent.append(url)
            key = "adgroup_id" if next_kind == "ADGROUP" else "smart_plus_ad_id"
            return HTTPResponse(
                body=json.dumps(
                    {"code": 0, "request_id": "resumed", "data": {key: "resumed-child"}}
                ).encode(),
                status=200,
            )

        monkeypatch.setattr("urllib3.PoolManager.request", resume)
        assert (
            execution.process_step(
                database_engine=engine,
                redis_client=redis,
                context=context,
                step_id=child.id,
                revision=child.dispatch_revision,
            )
            == "SUCCEEDED"
        )
        assert len(sent) == 1
