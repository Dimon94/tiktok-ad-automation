"""R10/T15 through HTTP recovery, original tasks and SDK transport."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds import recovery, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
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
