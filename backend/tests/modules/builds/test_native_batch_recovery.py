"""R10/T15 through HTTP recovery, original tasks and SDK transport."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds import recovery, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.recovery_api import router
from app.modules.builds.tasks import deliver_step, finish_delivery


def recover_batch(client, engine, context, redis, monkeypatch, sid, creates):
    client.app.include_router(router)
    baseline = deepcopy(creates)
    with Session(engine) as db:
        source = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == sid, ExecutionStep.status == "UNKNOWN"
            )
        ).one()
        source_id, unit_id = source.id, source.unit_id
        body, digest = deepcopy(source.request_body), source.request_body_digest
        before = submissions.get_submission(db, context=context, submission_id=sid)
        assert before.status == "NEEDS_REVIEW"
        assert sum(p.status == "COMPLETED" for p in before.native_plans) == 1

    def siblings(db):
        return sorted(
            (str(s.id), s.remote_id, s.request_body_digest)
            for s in db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid, ExecutionStep.unit_id != unit_id
                )
            ).all()
        )

    with Session(engine) as db:
        original_siblings = siblings(db)
    read_calls = []
    for mode in ("initial", "empty", "many", "missing", "unique"):
        row = {
            **body,
            "campaign_id": "recovered-original",
            "campaign_type": "REGULAR_CAMPAIGN",
        }
        row.pop("request_id", None)
        rows = [row]
        if mode in {"initial", "empty"}:
            rows = []
        elif mode == "many":
            rows.append({**row, "campaign_id": "ambiguous-second"})
        elif mode == "missing":
            del row["budget"]

        def transport(_pool, method, url, *, rows=rows, **_kwargs):
            assert method == "GET", "R10 recovery must never send a write"
            read_calls.append(url)
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 0,
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

        monkeypatch.setattr("urllib3.PoolManager.request", transport)
        # Only scheduling clocks advance; UNKNOWN came from a real SDK send.
        with Session(engine) as db, db.begin():
            source = db.get(ExecutionStep, source_id)
            if source.lease_expires_at is not None:
                source.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            source.due_at = datetime.now(UTC) - timedelta(seconds=1)
            db.add(source)
        if mode == "initial":
            with Session(engine) as db:
                revision = db.get(ExecutionStep, source_id).dispatch_revision
            finish_delivery(
                database_engine=engine,
                context=context,
                step_id=source_id,
                revision=revision,
            )
            with Session(engine) as db:
                revision = db.get(ExecutionStep, source_id).dispatch_revision
            deliver_step(
                database_engine=engine,
                redis_client=redis,
                context=context,
                payload={"step_id": str(source_id), "revision": revision},
                reconcile=True,
            )
            continue
        key = str(uuid4())
        url = f"/tenants/{context.tenant_id}/submissions/{sid}/reconcile"
        response = client.post(url, json={"request_id": key})
        assert response.status_code == 202, response.text
        assert client.post(url, json={"request_id": key}).json() == response.json()
        payload = {"recovery_id": response.json()["recovery_id"], "revision": 0}
        for _ in range(2):
            recovery.process_recovery(
                database_engine=engine, context=context, payload=payload
            )
        with Session(engine) as db:
            revision = db.get(ExecutionStep, source_id).dispatch_revision
        delivery = {"step_id": str(source_id), "revision": revision}

        def deliver(payload):
            deliver_step(
                database_engine=engine,
                redis_client=redis,
                context=context,
                payload=payload,
                reconcile=True,
            )

        deliver({**delivery, "revision": max(0, revision - 1)})
        deliver(delivery)
        count = len(read_calls)
        deliver(delivery)
        assert len(read_calls) == count
        with Session(engine) as db:
            source = db.get(ExecutionStep, source_id)
            assert (
                source.request_body,
                source.request_body_digest,
                source.unit_id,
            ) == (body, digest, unit_id)
            assert source.status == ("SUCCEEDED" if mode == "unique" else "UNKNOWN"), (
                f"R10 candidate {mode}"
            )
            assert source.remote_id == (
                "recovered-original" if mode == "unique" else None
            ), mode
            current = submissions.get_submission(db, context=context, submission_id=sid)
            assert current.status == (
                "RUNNING" if mode == "unique" else "NEEDS_REVIEW"
            ), current
            assert original_siblings == siblings(db)
            assert (
                db.exec(
                    select(StepEvidence).where(
                        StepEvidence.step_id == source_id,
                        StepEvidence.conclusion == "READBACK_PAGE",
                    )
                ).first()
                is not None
            )
        assert creates == baseline


def recover_terminal(
    client,
    engine,
    context,
    redis,
    monkeypatch,
    sid,
    creates,
    objects,
    expected,
    preview_id,
    *,
    browser_bc=None,
):
    """T16/T17：最后一个实际发送的 Ad，经原只读任务闭合。"""
    client.app.include_router(router)
    baseline = deepcopy(creates)
    with Session(engine) as db:
        source = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == sid, ExecutionStep.status == "UNKNOWN"
            )
        ).one()
        source_id = source.id
        body, digest, unit_id = (
            deepcopy(source.request_body),
            source.request_body_digest,
            source.unit_id,
        )
        siblings = [
            (x.id, x.remote_id, x.request_body_digest, x.status)
            for x in db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid, ExecutionStep.unit_id != unit_id
                )
            ).all()
        ]
        original_stamp = deepcopy(db.get(Submission, sid).authorization_stamp)
    row = next(
        deepcopy(r)
        for r in objects.values()
        if r.get("ad_name") == body.get("ad_name")
        and r.get("advertiser_id") == body["advertiser_id"]
        and "smart_plus_ad_id" in r
    )
    reads = []
    rows = []

    def transport(_pool, method, url, **_kwargs):
        assert method == "GET", "R10 terminal recovery wrote remotely"
        reads.append(url)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
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

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    with Session(engine) as db, db.begin():
        source = db.get(ExecutionStep, source_id)
        if source.lease_expires_at is not None:
            source.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        source.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = source.dispatch_revision
        db.add(source)
    finish_delivery(
        database_engine=engine, context=context, step_id=source_id, revision=revision
    )
    with Session(engine) as db:
        revision = db.get(ExecutionStep, source_id).dispatch_revision
    deliver_step(
        database_engine=engine,
        redis_client=redis,
        context=context,
        payload={"step_id": str(source_id), "revision": revision},
        reconcile=True,
    )
    rows = [row]

    def drive(recovery_id):
        payload = {"recovery_id": str(recovery_id), "revision": 0}
        recovery.process_recovery(
            database_engine=engine, context=context, payload=payload
        )
        with Session(engine) as db:
            revision = db.get(ExecutionStep, source_id).dispatch_revision
        delivery = {"step_id": str(source_id), "revision": revision}
        deliver_step(
            database_engine=engine,
            redis_client=redis,
            context=context,
            payload=delivery,
            reconcile=True,
        )
        # 认领与完整三级 readback 是原有两个任务阶段，均只读。
        with Session(engine) as db:
            pending = db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid,
                    ExecutionStep.kind == "READBACK",
                    ExecutionStep.status != "SUCCEEDED",
                )
            ).all()
        from app.modules.builds import reconciliation

        for step in pending:
            reconciliation.process_reconciliation(
                database_engine=engine,
                redis_client=redis,
                context=context,
                step_id=step.id,
                revision=step.dispatch_revision,
            )
        read_count = len(reads)
        recovery.process_recovery(
            database_engine=engine, context=context, payload=payload
        )
        deliver_step(
            database_engine=engine,
            redis_client=redis,
            context=context,
            payload=delivery,
            reconcile=True,
        )
        assert len(reads) == read_count

    if not browser_bc:
        # 少量传输代表例：字段缺失、错误配对、额外 addon、兄弟账户。
        candidates = []
        missing = deepcopy(row)
        missing.pop("creative_list")
        candidates.append(missing)
        for key, value in (
            ("video_info", {"video_id": "wrong-video"}),
            ("image_info", [{"web_uri": "wrong-cover"}]),
            ("identity_id", "wrong-identity"),
        ):
            wrong = deepcopy(row)
            wrong["creative_list"][0]["creative_info"][key] = value
            candidates.append(wrong)
        duplicate = deepcopy(row)
        duplicate["creative_list"].append(deepcopy(duplicate["creative_list"][0]))
        candidates.append(duplicate)
        static = deepcopy(row)
        static["call_to_action_list"] = [{"call_to_action": "SHOP_NOW"}]
        candidates.append(static)
        wrong_text = deepcopy(row)
        wrong_text["ad_text_list"] = [{"ad_text": "unapproved text"}]
        candidates.append(wrong_text)
        addon = deepcopy(row)
        addon["interactive_add_on_list"] = [{"interactive_add_on_id": "unexpected"}]
        candidates.append(addon)
        wrong_account = deepcopy(row)
        wrong_account["advertiser_id"] = "actual-account"
        candidates.append(wrong_account)
        for candidate_index, candidate in enumerate(candidates):
            rows = [candidate]
            response = client.post(
                f"/tenants/{context.tenant_id}/submissions/{sid}/reconcile",
                json={"request_id": str(uuid4())},
            )
            assert response.status_code == 202, response.text
            drive(response.json()["recovery_id"])
            with Session(engine) as db:
                assert db.get(ExecutionStep, source_id).status == "UNKNOWN", (
                    f"R10 incomplete Ad candidate claimed: {candidate_index}"
                )
                assert (
                    submissions.get_submission(
                        db, context=context, submission_id=sid
                    ).status
                    == "NEEDS_REVIEW"
                )
            assert creates == baseline
        rows = [deepcopy(row)]
        rows[0]["creative_list"].reverse()
    if browser_bc:
        from tests.modules.builds.native_browser_http import submit_in_browser

        submit_in_browser(
            engine,
            context,
            browser_bc,
            preview_id,
            mode="batch-recovery",
            submission_id=sid,
            drive=drive,
        )
    else:
        response = client.post(
            f"/tenants/{context.tenant_id}/submissions/{sid}/reconcile",
            json={"request_id": str(uuid4())},
        )
        assert response.status_code == 202, response.text
        drive(response.json()["recovery_id"])
    with Session(engine) as db:
        current = submissions.get_submission(db, context=context, submission_id=sid)
        assert current.status == expected, (
            "R13 terminal recovery must reflect complete plan matches"
        )
        source = db.get(ExecutionStep, source_id)
        assert source.status == "SUCCEEDED"
        assert (source.request_body, source.request_body_digest, source.unit_id) == (
            body,
            digest,
            unit_id,
        )
        assert all(p.remote_ids for p in current.native_plans if p.status != "EXCLUDED")
        assert db.get(Submission, sid).authorization_stamp == original_stamp
        assert siblings == [
            (x.id, x.remote_id, x.request_body_digest, x.status)
            for x in db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid, ExecutionStep.unit_id != unit_id
                )
            ).all()
        ]
        assert (
            db.exec(
                select(StepEvidence).where(
                    StepEvidence.step_id == source_id,
                    StepEvidence.conclusion == "RECONCILED",
                )
            ).first()
            is not None
        )
    key = uuid4()
    for request_id in (key, key, uuid4()):
        with Session(engine) as db, db.begin():
            receipt = submissions.submit_preview(
                db, context=context, preview_id=preview_id, request_id=request_id
            )
            assert receipt.submission_id == sid and receipt.status == expected
    assert creates == baseline
