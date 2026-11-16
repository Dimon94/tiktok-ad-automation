"""同一提交链的独立平台响应；常量事实不从请求复制，动态名称/排期/抽选文案单独绑定。"""

import json
from types import SimpleNamespace

from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds import reconciliation, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence


def reconcile_created(
    engine, context, redis_client, monkeypatch, submission_id, creates, scenario
):
    from urllib.parse import parse_qs, urlsplit

    rows = observed_rows(creates)
    if scenario == "difference":
        rows["remote-4"]["creative_list"][0]["creative_info"]["video_info"][
            "video_id"
        ] = "wrong-video"
    if scenario == "rejected":
        rows["remote-4"]["secondary_status"] = "AUDIT_REJECTED"
    if scenario == "absent":
        del rows["remote-4"]
    if scenario == "distribution":
        rows["remote-3"]["drama_selling_setting"] = {
            "type": "DISTRIBUTION",
            "distribution_link": "https://example.test/distribution",
        }
    if scenario == "static_cta":
        rows["remote-4"]["call_to_action_list"] = [{"call_to_action": "SHOP_NOW"}]
    if scenario == "addon":
        rows["remote-4"]["interactive_add_on_list"] = [
            {"interactive_add_on_id": "unexpected"}
        ]
    reads = []

    def request(_pool, method, url, **kw):
        assert method == "GET", "readback must never create"
        args = dict(kw.get("fields") or parse_qs(urlsplit(url).query))
        reads.append(args)
        filtering = args.get("filtering", "{}")
        if isinstance(filtering, str):
            filtering = json.loads(filtering)
        key = next(
            k
            for k in ("smart_plus_ad_ids", "adgroup_ids", "campaign_ids")
            if k in filtering
        )
        rid = filtering[key][0]
        found = [rows[rid]] if rid in rows else []
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "request_id": "observed-" + rid,
                    "data": {
                        "list": found,
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_page": 1,
                            "total_number": len(found),
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
        steps = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == submission_id,
                ExecutionStep.kind == "READBACK",
            )
        ).all()
    expected_reads = 1 if scenario == "partial" else 8
    assert len(steps) == expected_reads
    for step in steps:
        result = reconciliation.process_reconciliation(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            step_id=step.id,
            revision=step.dispatch_revision,
        )
        assert result.state in {"SUCCEEDED", "MISMATCH", "PENDING"}, (step.id, result)
    with Session(engine) as db, db.begin():
        summary = submissions.get_submission(
            db, context=context, submission_id=submission_id
        )
        assert summary.status == (
            "COMPLETED"
            if scenario == "match"
            else "PARTIAL"
            if scenario == "partial"
            else "NEEDS_REVIEW"
        )
        evidence = db.exec(
            select(StepEvidence).where(
                StepEvidence.submission_id == submission_id,
                StepEvidence.conclusion == "READBACK_PAGE",
            )
        ).all()
        assert len(evidence) == expected_reads
        assert len(reads) == expected_reads
        if scenario in {
            "difference",
            "rejected",
            "distribution",
            "static_cta",
            "addon",
        }:
            assert any(
                e.summary["candidate"]["comparison"] == "MISMATCH" for e in evidence
            )

    from app.modules.builds.dispatch import finalize_submission, process_unit
    from app.modules.builds.execution_models import Submission, SubmissionUnit

    for _ in range(3):
        with Session(engine) as db:
            unit = db.exec(
                select(SubmissionUnit).where(
                    SubmissionUnit.submission_id == submission_id
                )
            ).one()
            if unit.dispatch_id is None:
                break
            revision = unit.dispatch_revision
        process_unit(
            database_engine=engine,
            context=context,
            payload={"unit_id": str(steps[0].unit_id), "revision": revision},
        )
    finalize_submission(
        database_engine=engine, context=context, submission_id=submission_id
    )
    with Session(engine) as db:
        current = submissions.get_submission(
            db, context=context, submission_id=submission_id
        )
        assert db.get(Submission, submission_id).status == current.status
        if scenario == "absent":
            # 上面的单元唤醒已重新投递缺失对象回读；旧摘要属于排队前。
            assert current.status == "RUNNING"
            pending_reads = db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == submission_id,
                    ExecutionStep.kind == "READBACK",
                    ExecutionStep.status == "QUEUED",
                )
            ).all()
            assert len(pending_reads) == 1
        else:
            assert current.status == summary.status
        if scenario in {
            "difference",
            "rejected",
            "distribution",
            "static_cta",
            "addon",
        }:
            page = submissions.get_submission_steps(
                db, context=context, submission_id=submission_id
            )
            assert any(item.readback_differences for item in page.items)


def observed_rows(creates):
    from tests.modules.builds.test_native_readback import observed_group

    rows = {}
    for index, (url, body) in enumerate(creates, 1):
        rid = f"remote-{index}"
        if "/campaign/" in url:
            row = {
                "campaign_id": rid,
                "advertiser_id": "actual-account",
                "campaign_name": body["campaign_name"],
                "objective_type": "APP_PROMOTION",
                "app_promotion_type": "MINIS",
                "campaign_type": "REGULAR_CAMPAIGN",
                "budget": "1000.01",
                "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
                "budget_optimize_on": True,
                "operation_status": "ENABLE",
            }
        elif "/adgroup/" in url:
            row = observed_group()
            row.update(
                advertiser_id="actual-account",
                campaign_id="remote-2",
                adgroup_id=rid,
                adgroup_name=body["adgroup_name"],
                native_series_id="exact-series",
                schedule_start_time=body["schedule_start_time"],
                roas_bid="1.08",
            )
            # 账户成人准入是既有冻结 wire 的独立约束。
            row["targeting_spec"] = {
                "location_ids": ["6252001"],
                "spc_audience_age": "OVER_EIGHTEEN",
            }
        elif "/ad/create/" in url:
            row = {
                "smart_plus_ad_id": rid,
                "advertiser_id": "actual-account",
                "adgroup_id": "remote-3",
                "ad_name": body["ad_name"],
                "operation_status": "ENABLE",
                "ad_text_list": [{"ad_text": body["ad_text_list"][0]["ad_text"]}],
                "ad_configuration": {"call_to_action_id": "remote-1"},
                "creative_list": [
                    {
                        "creative_info": {
                            "identity_type": "TT_USER",
                            "identity_id": "owner",
                            "ad_format": "SINGLE_VIDEO",
                            "video_info": {"video_id": f"target-{i}"},
                            "image_info": [{"web_uri": f"cover-{i}"}],
                        }
                    }
                    for i in reversed(range(6))
                ],
            }
        else:
            continue
        rows[rid] = row
    return rows
