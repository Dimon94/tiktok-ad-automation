"""Independent execution audit against real PostgreSQL and official SDK wire."""

import json

import pytest
from sqlalchemy import text
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.integrations.tiktok.contracts.builds import VideoCreative
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_execution import run


def success_wire(monkeypatch):
    calls = []

    def request(_pool, method, url, **kwargs):
        assert method == "POST"
        body = json.loads(kwargs["body"])
        kind = "cta" if "portfolio" in url else url.split("/")[-3]
        key = {
            "cta": "creative_portfolio_id",
            "campaign": "campaign_id",
            "adgroup": "adgroup_id",
            "ad": "smart_plus_ad_id",
        }[kind]
        remote_id = f"actual-{kind}-{len(calls)}"
        calls.append((kind, body, remote_id))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {key: remote_id, "operation_status": "ENABLE"},
                    "request_id": "review-offline",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    return calls


def test_review_ad_binds_actual_cta_in_documented_ad_configuration(
    executable, redis_client, monkeypatch
):
    """Official doc1843317390059522: CTA is nested in ad_configuration."""
    calls = success_wire(monkeypatch)
    for kind in ("MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"):
        run(executable, redis_client, kind)
    cta_id = next(remote_id for kind, _, remote_id in calls if kind == "cta")
    ads = [body for kind, body, _ in calls if kind == "ad"]
    assert ads
    for body in ads:
        assert body.get("ad_configuration", {}).get("call_to_action_id") == cta_id
        assert "call_to_action_id" not in body


def test_review_database_commit_failure_preserves_received_cta_id(
    executable, redis_client, monkeypatch
):
    """PostgreSQL defers a real rejection until commit, rolling back the receipt."""
    db, _, ids = executable
    with db.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION review_reject_created_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.kind='CTA' AND NEW.status='SUCCEEDED' THEN
                RAISE EXCEPTION 'review deferred receipt rejection' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER review_deferred_receipt
            AFTER UPDATE ON execution_step DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION review_reject_created_receipt()
        """)
        )
    calls = success_wire(monkeypatch)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    assert len(calls) == 1
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "UNKNOWN" and step.remote_id is None
        evidence = session.exec(
            select(StepEvidence).where(StepEvidence.step_id == step.id)
        ).all()
        # A known CTA ID must survive in append-only evidence because there is no
        # unknown-ID list endpoint. Recovery may not discard this successful GET key.
        assert any(row.summary.get("remote_id") == calls[0][2] for row in evidence)
        saved_body = step.request_body
    # Once the transient database failure is gone, use that receipt for the only
    # supported CTA GET. No unknown-ID search and no second create is necessary.
    from datetime import UTC, datetime, timedelta

    with db.begin() as connection:
        connection.execute(
            text("DROP TRIGGER review_deferred_receipt ON execution_step")
        )
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["CTA"][0])
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(step)
    from app.modules.builds import reconciliation

    bounded_readback(monkeypatch)
    reads = []

    def read(_pool, method, url, **kwargs):
        assert method == "GET" and url.endswith("/creative/portfolio/get/")
        query = dict(kwargs["fields"])
        assert query["creative_portfolio_id"] == calls[0][2]
        reads.append(query)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "creative_portfolio_id": calls[0][2],
                        "creative_portfolio_type": "CTA",
                        "advertiser_id": saved_body["advertiser_id"],
                        "portfolio_content": saved_body["portfolio_content"],
                    },
                    "request_id": "recovered",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", read)
    result = reconciliation.process_reconciliation(
        database_engine=db,
        redis_client=redis_client,
        context=executable[1],
        step_id=ids["CTA"][0],
        revision=0,
    )
    assert result.state == "SUCCEEDED" and len(reads) == 1 and len(calls) == 1


def test_review_fairness_redis_outage_defers_unsent_step(executable, monkeypatch):
    import socket

    from redis import Redis

    db, _, ids = executable
    calls = success_wire(monkeypatch)
    # An actually refused local socket connection, with no fake workflow/Redis
    # result. Bound-but-not-listening prevents another process taking the port.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        client = Redis(
            host="127.0.0.1",
            port=unavailable.getsockname()[1],
            db=1,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
        )
        try:
            run(executable, client, "CTA")
        finally:
            client.close()
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.request_body is None and step.remote_id is None
        assert step.error_code == "tiktok_local_resources_unavailable"
        assert step.status == "PENDING"


@pytest.mark.parametrize("provider_verified", [False, True])
def test_review_real_scene_checks_provider_and_missing_evidence(
    executable, redis_client, monkeypatch, provider_verified
):
    from app.modules.builds import execution, scene

    db, _, ids = executable
    calls = success_wire(monkeypatch)
    # 预览现在必须有目录证据；本例显式使证据过期，验证执行前必须重核。
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.scene_job_models import SceneJob

    with Session(db) as session, session.begin():
        for job in session.exec(select(SceneJob)).all():
            job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.add(job)
    if provider_verified:
        from uuid import uuid4

        from app.modules.providers.models import ProviderApplication, ProviderConnection

        with Session(db) as session, session.begin():
            provider = session.exec(select(ProviderConnection)).one()
            provider.verification_token = uuid4()
            app = session.exec(select(ProviderApplication)).one()
            app.tiktok_minis_id = "minis-1"
            app.channel_config = {
                **app.channel_config,
                "verification_token": str(provider.verification_token),
            }
            session.add_all([provider, app])
    monkeypatch.setattr(execution, "read_scene_context", scene.read_scene_context)
    run(executable, redis_client, "CTA")
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert (step.status, step.error_code) == (
            ("PENDING", "scene_refresh_required")
            if provider_verified
            else ("FAILED", "scene_link_unavailable")
        )
        assert step.request_body is None


def test_review_real_metadata_change_after_admission_blocks_old_budget_currency(
    executable, redis_client, monkeypatch
):
    from contextlib import contextmanager

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.builds import execution

    db, context, ids = executable
    calls = success_wire(monkeypatch)
    original = execution.admitted_build_call

    @contextmanager
    def change(*args, **kwargs):
        with original(*args, **kwargs):
            with Session(db) as session, session.begin():
                account = session.get(
                    AdvertiserAccount, (context.tenant_id, "account-A")
                )
                account.currency = "EUR"
                session.add(account)
            yield

    monkeypatch.setattr(execution, "admitted_build_call", change)
    run(executable, redis_client, "CTA")
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "FAILED" and step.error_code == "account_metadata_changed"
        assert step.request_body is None


def test_review_scene_business_change_after_admission_does_not_arm(
    executable, redis_client, monkeypatch
):
    from contextlib import contextmanager
    from dataclasses import replace

    from app.modules.builds import execution

    db, _, ids = executable
    calls = success_wire(monkeypatch)
    current_scene, admission = (
        execution.read_scene_context,
        execution.admitted_build_call,
    )

    @contextmanager
    def change(*args, **kwargs):
        with admission(*args, **kwargs):

            def changed(*scene_args, **scene_kwargs):
                current = current_scene(*scene_args, **scene_kwargs)
                return replace(current, cta_fields={"asset_ids": ["changed"]})

            monkeypatch.setattr(execution, "read_scene_context", changed)
            yield

    monkeypatch.setattr(execution, "admitted_build_call", change)
    run(executable, redis_client, "CTA")
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.error_code == "scene_intent_changed" and step.request_body is None


def test_scene_policy_metadata_update_preserves_frozen_execution(
    executable, redis_client, monkeypatch
):
    from dataclasses import replace

    from app.modules.builds import execution

    calls = success_wire(monkeypatch)
    original = execution.read_scene_context

    def refreshed(*args, **kwargs):
        scene = original(*args, **kwargs)
        return replace(
            scene,
            field_constraints={
                **scene.field_constraints,
                "revision": "new-policy-revision",
                "targeting_optimization_modes": ["AUTOMATIC"],
            },
        )

    monkeypatch.setattr(execution, "read_scene_context", refreshed)
    run(executable, redis_client, "CTA")
    assert len(calls) == 1
    with Session(executable[0]) as session:
        step = session.get(ExecutionStep, executable[2]["CTA"][0])
        assert step.status == "SUCCEEDED"


def test_review_pre_arm_commit_rejection_never_reaches_transport(
    executable, redis_client, monkeypatch
):
    db, _, ids = executable
    with db.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION review_reject_arm() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.phase='REQUEST_ARMED' THEN
                RAISE EXCEPTION 'review deferred arm rejection' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER review_deferred_arm
            AFTER UPDATE ON execution_step DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION review_reject_arm()
        """)
        )
    calls = success_wire(monkeypatch)
    run(executable, redis_client, "CTA")
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.request_body is None and step.remote_id is None
        # bounded_session 把提交异常归为本地资源不可用；未 arm、未发送可重排。
        assert step.status == "PENDING"
        assert step.error_code == "tiktok_local_resources_unavailable"


@pytest.mark.parametrize(
    "cleanup_failure,known_id", [(False, False), (True, False), (True, True)]
)
def test_review_actual_late_sdk_receipt_preserves_new_owner_nonce(
    executable, redis_client, monkeypatch, cleanup_failure, known_id
):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    db, _, ids = executable
    identity, replacement = ids["CTA"][0], uuid4()
    if cleanup_failure:
        import urllib3

        original_clear = urllib3.PoolManager.clear

        def failing_cleanup(pool):
            original_clear(pool)
            raise RuntimeError("review cleanup failure")

        monkeypatch.setattr(urllib3.PoolManager, "clear", failing_cleanup)
    writes = []

    def request(_pool, method, _url, **_kwargs):
        writes.append(method)
        with Session(db) as session, session.begin():
            step = session.exec(
                select(ExecutionStep)
                .where(ExecutionStep.id == identity)
                .with_for_update(nowait=True)
            ).one()
            assert step.phase == "REQUEST_ARMED" and step.attempt == 1
            step.status = "SUCCEEDED" if known_id else "UNKNOWN"
            if known_id:
                step.remote_id, step.phase = "new-actual", "DONE"
            step.attempt = 2
            save_attempt_context(session, step=step)
            step.lease_token = replacement
            step.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
            session.add(step)
        return HTTPResponse(
            body=b'{"code":0,"data":{"creative_portfolio_id":"late-actual"},"request_id":"late"}',
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    assert writes == ["POST"]
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == ("SUCCEEDED" if known_id else "UNKNOWN")
        assert step.remote_id == ("new-actual" if known_id else None)
        assert step.lease_token == replacement and step.attempt == 2
        late = session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == identity,
                StepEvidence.conclusion == "LATE_CREATED",
            )
        ).all()
        assert len(late) == (2 if cleanup_failure else 1)
        assert all(
            row.summary["remote_id"] == "late-actual"
            and row.attempt == 1
            and row.lease_token != replacement
            for row in late
        )
        if known_id:
            assert step.mismatch


@pytest.mark.parametrize("copy", ["", "  \t\n", "a" * 101, "剧" * 101])
def test_review_ad_copy_application_policy_rejects_blank_or_over_100(copy):
    from app.core.errors import DomainError
    from app.modules.builds.request_compiler import ad_assets

    with pytest.raises(DomainError):
        ad_assets(
            [VideoCreative(kind="VIDEO", video_id="target", cover_image_id="cover")],
            text=copy,
            url="https://example.test/frozen",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity",
                "identity_authorized_bc_id": "bc",
            },
        )


def test_review_ad_copy_application_policy_counts_characters_not_bytes():
    from app.modules.builds.request_compiler import ad_assets

    body = ad_assets(
        [VideoCreative(kind="VIDEO", video_id="target", cover_image_id="cover")],
        text="剧" * 100,
        url="https://example.test/frozen",
        identity={
            "identity_type": "BC_AUTH_TT",
            "identity_id": "identity",
            "identity_authorized_bc_id": "bc",
        },
    )
    assert body["ad_text_list"] == [{"ad_text": "剧" * 100}]


def bounded_readback(monkeypatch):
    from types import SimpleNamespace

    from app.modules.builds import reconciliation

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


def test_review_actual_ad_execution_body_passes_strict_readback(
    executable, redis_client, monkeypatch
):
    from app.modules.builds import reconciliation

    db, context, ids = executable
    calls = success_wire(monkeypatch)
    for kind in ("MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"):
        run(executable, redis_client, kind)
    with Session(db) as session:
        source = session.get(ExecutionStep, ids["AD"][0])
        expected, actual_id = source.request_body, source.remote_id
        # 显式核实原创建仍受同样字段契约保护，不依赖新图中已移除的核查节点。
        readback_id = source.id
    bounded_readback(monkeypatch)
    reads = []

    def read(_pool, method, url, **_kwargs):
        assert method == "GET" and url.endswith("/smart_plus/ad/get/")
        reads.append(url)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "request_id": "ad-read",
                    "data": {
                        "list": [
                            {
                                **expected,
                                "smart_plus_ad_id": actual_id,
                                "secondary_status": "AD_STATUS_AUDIT",
                            }
                        ],
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_number": 1,
                            "total_page": 1,
                        },
                    },
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", read)
    result = reconciliation.process_reconciliation(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        step_id=readback_id,
        revision=0,
    )
    assert result.state == "SUCCEEDED" and len(reads) == 1
    assert len([kind for kind, _, _ in calls if kind == "ad"]) == 2
