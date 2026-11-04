"""Real PostgreSQL requests, cursor commits and exact original dispatches."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.builds import recovery, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.builds.recovery_models import SubmissionRecovery
from app.modules.builds.routes import save_attempt_context
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_execution import executable as executable


def setup_failure(env, *, kind="CTA", status="FAILED", **changes):
    db, context, ids = env
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids[kind][0])
        step.status, step.phase, step.error_code = (
            status,
            "DONE",
            "admission_unavailable",
        )
        step.dispatch_id = None
        for key, value in changes.items():
            setattr(step, key, value)
        session.add(step)
        return step.submission_id, step.id


def request(env, submission_id, *, kind="RETRY", request_id=None):
    db, context, _ = env
    with Session(db) as session, session.begin():
        return recovery.request_recovery(
            session,
            context=context,
            submission_id=submission_id,
            request_id=request_id or uuid4(),
            kind=kind,
        )


def run(env, receipt, revision=0):
    recovery.process_recovery(
        database_engine=env[0],
        context=env[1],
        payload={"recovery_id": str(receipt.recovery_id), "revision": revision},
    )


def test_request_replay_original_and_read_only_current_progress(executable):
    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    receipt = request(executable, submission_id)
    assert receipt.state == "QUEUED" and receipt.scheduled_count == 0
    run(executable, receipt)
    with Session(db) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert (
            recovery.get_request(
                session, context=context, request_id=receipt.request_id
            )
            == receipt
        )
        current = recovery.get_recovery(
            session, context=context, recovery_id=receipt.recovery_id
        )
        assert (current.state, current.scheduled_count) == ("COMPLETED", 1)
        step = session.get(ExecutionStep, step_id)
        dispatch = session.get(PendingDispatch, step.dispatch_id)
        assert dispatch.actor_id == context.actor_id
        assert dispatch.payload == {"step_id": str(step_id), "revision": 1}
        assert dispatch.task_name == "builds.execute_step"
    assert request(executable, submission_id, request_id=receipt.request_id) == receipt
    run(executable, receipt)
    with Session(db) as session:
        assert session.get(ExecutionStep, step_id).dispatch_revision == 1


def test_read_recovery_does_not_require_new_create_permission(executable):
    from app.modules.accounts.connection_models import ConnectionAuthorization
    from app.modules.accounts.models import BCAccountAccess

    db, context, _ = executable
    submission_id, step_id = setup_failure(executable, status="UNKNOWN")
    with Session(db) as session, session.begin():
        for auth in session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.tenant_id == context.tenant_id
            )
        ).all():
            auth.permission_summary = {
                "read_authorized": True,
                "build_authorized": None,
            }
            session.add(auth)
        for grant in session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id
            )
        ).all():
            grant.can_build = False
            grant.permission_state = "UNKNOWN"
            session.add(grant)
    receipt = request(executable, submission_id, kind="RECONCILE")
    run(executable, receipt)
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert (
            session.get(PendingDispatch, step.dispatch_id).task_name
            == "builds.reconcile_step"
        )
        assert step.status == "UNKNOWN"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "UNKNOWN"},
        {"status": "SUCCEEDED", "remote_id": "actual-id"},
        {
            "request_body": {"advertiser_id": "account-A"},
            "request_body_digest": "a" * 64,
        },
        {
            "phase": "REQUEST_ARMED",
            "request_body": {"advertiser_id": "account-A"},
            "request_body_digest": "b" * 64,
        },
        {
            "lease_token": uuid4(),
        },
        {"error_code": "new_preview_required"},
    ],
)
def test_unsafe_or_changed_intent_never_retries(executable, changes):
    if "lease_token" in changes:
        # Parametrization happens at collection; long CI runs must still exercise
        # a live lease when this case actually executes.
        changes = {
            **changes,
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
        }
    submission_id, _ = setup_failure(executable, **changes)
    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request(executable, submission_id)


@pytest.mark.parametrize("business_changed", [False, True])
def test_scene_mismatch_retry_rechecks_current_frozen_intent(
    executable, monkeypatch, business_changed
):
    from dataclasses import replace

    from app.modules.builds import execution

    submission_id, step_id = setup_failure(
        executable, error_code="scene_intent_changed"
    )
    original = execution.read_scene_context

    def refreshed(*args, **kwargs):
        scene = original(*args, **kwargs)
        return replace(
            scene,
            field_constraints={**scene.field_constraints, "revision": "new-policy"},
            cta_fields={"asset_ids": ["changed"]}
            if business_changed
            else scene.cta_fields,
        )

    monkeypatch.setattr(execution, "read_scene_context", refreshed)
    receipt = request(executable, submission_id)
    run(executable, receipt)
    with Session(executable[0]) as session:
        step = session.get(ExecutionStep, step_id)
        assert step.request_body is None
        assert step.status == ("FAILED" if business_changed else "QUEUED")
        assert bool(step.dispatch_id) is (not business_changed)


def test_historical_armed_evidence_also_prevents_retry(executable):
    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    with Session(db) as session, session.begin():
        save_attempt_context(
            session, step=session.get(ExecutionStep, step_id), attempt=1
        )
        session.add(
            StepEvidence(
                tenant_id=context.tenant_id,
                submission_id=submission_id,
                step_id=step_id,
                attempt=1,
                conclusion="REQUEST_ARMED",
                summary={"body_digest": "c" * 64},
            )
        )
    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request(executable, submission_id)


def test_unknown_preserves_reconciliation_cursor_and_only_queues_read(executable):
    db, _, _ = executable
    progress = {
        "reconciliation": {"next_page": 2},
        "reconciliation_delivery": {"generation": 3},
    }
    submission_id, step_id = setup_failure(
        executable, status="UNKNOWN", resolved=progress
    )
    receipt = request(executable, submission_id, kind="RECONCILE")
    run(executable, receipt)
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert step.status == "UNKNOWN" and step.request_body is None
        assert all(step.resolved[key] == value for key, value in progress.items())
        dispatch = session.get(PendingDispatch, step.dispatch_id)
        assert dispatch.task_name == "builds.reconcile_step"
        assert step.resolved["dispatch_reconciliation_done"] is False


def test_concurrent_permanent_request_and_mismatched_intent(executable):
    db, context, _ = executable
    submission_id, _ = setup_failure(executable)
    request_id, barrier = uuid4(), Barrier(2)

    def call():
        barrier.wait(timeout=10)
        return request(executable, submission_id, request_id=request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [
            future.result(timeout=15)
            for future in [pool.submit(call), pool.submit(call)]
        ]
    assert a == b
    with Session(db) as session:
        jobs = session.exec(
            select(SubmissionRecovery).where(
                SubmissionRecovery.tenant_id == context.tenant_id
            )
        ).all()
        assert len(jobs) == 1
    for sub, kind in [(uuid4(), "RETRY"), (submission_id, "RECONCILE")]:
        with pytest.raises(
            DomainError, check=lambda e: e.code == "idempotency_conflict"
        ):
            request(executable, sub, kind=kind, request_id=request_id)


def test_revoked_original_actor_stops_job_but_saved_receipt_remains_readable(
    executable,
):
    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    receipt = request(executable, submission_id)
    with Session(db) as session, session.begin():
        member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
        member.role = "viewer"
        session.add(member)
    run(executable, receipt)
    with Session(db) as session:
        current = recovery.get_recovery(
            session, context=context, recovery_id=receipt.recovery_id
        )
        assert current.state == "FAILED" and current.reason_code == "action_forbidden"
        assert (
            recovery.get_request(
                session, context=context, request_id=receipt.request_id
            )
            == receipt
        )
        assert session.get(ExecutionStep, step_id).status == "FAILED"
        assert not submissions.get_submission(
            session, context=context, submission_id=submission_id
        ).recovery.can_retry
    assert request(executable, submission_id, request_id=receipt.request_id) == receipt
    with pytest.raises(DomainError, check=lambda e: e.code == "action_forbidden"):
        request(executable, submission_id)


def test_request_and_exact_outbox_rollback_together(executable):
    db, context, _ = executable
    submission_id, _ = setup_failure(executable)
    request_id = uuid4()
    with Session(db) as session:
        recovery.request_recovery(
            session,
            context=context,
            submission_id=submission_id,
            request_id=request_id,
            kind="RETRY",
        )
        session.rollback()
    with Session(db) as session:
        with pytest.raises(DomainError, check=lambda e: e.code == "resource_not_found"):
            recovery.get_request(session, context=context, request_id=request_id)
        assert (
            session.exec(
                select(SubmissionRecovery).where(
                    SubmissionRecovery.request_id == request_id
                )
            ).all()
            == []
        )


def test_repair_preserves_unpublished_generation_id_and_backoff(executable):
    db, _, _ = executable
    submission_id, _ = setup_failure(executable)
    receipt = request(executable, submission_id)
    later = datetime.now(UTC) + timedelta(minutes=10)
    with Session(db) as session, session.begin():
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.available_at = later
        dispatch_id = dispatch.id
        session.add_all([job, dispatch])
    assert recovery.repair_recoveries(database_engine=db) == 1
    with Session(db) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert job.dispatch_id == dispatch_id and job.dispatch_revision == 0
        assert session.get(PendingDispatch, dispatch_id).available_at == later


def test_501_real_frozen_ads_resume_in_bounded_pages_and_duplicate_delivery(executable):
    from app.modules.builds import previews
    from app.modules.builds.drafts import create_draft, prepare_draft
    from app.modules.builds.execution_models import Submission
    from app.modules.builds.models import BuildDraft
    from app.modules.providers.models import ProviderApplication, ProviderConnection
    from app.modules.strategies.models import StrategyVersion
    from app.modules.strategies.service import append_version
    from tests.modules.builds.test_drafts import finish, ready_links
    from tests.modules.builds.test_previews import drain
    from tests.modules.materials.test_tenant_materials import material
    from tests.modules.strategies.test_versions import config

    db, context, ids = executable
    with Session(db) as session, session.begin():
        old = session.get(
            Submission, session.get(ExecutionStep, ids["CTA"][0]).submission_id
        )
        draft = session.get(BuildDraft, old.draft_id)
        version = session.get(StrategyVersion, draft.strategy_version_id)
        provider = ProviderConnection(
            tenant_id=context.tenant_id,
            kind="jiashu",
            display_name="large-fixture",
            encrypted_credentials="unused",
            status="active",
        )
        session.add(provider)
        session.flush()
        session.add(
            ProviderApplication(
                tenant_id=context.tenant_id,
                connection_id=provider.id,
                external_id="app-draft",
                name="large-fixture",
            )
        )
        session.flush()
        intent = {
            "bc_id": draft.bc_id,
            "provider_connection_id": provider.id,
            "application_id": draft.application_id,
            "link_config": draft.link_config,
            "strategy_version_id": append_version(
                session,
                context=context,
                strategy_id=version.strategy_id,
                config=config(max_materials_per_group=1, ads_per_group=30),
            ),
            "drama_lines": ["Moon"],
            "account_lines": ["account-A"],
        }
        from app.modules.builds.preview_models import BuildUnit
        from app.modules.materials.models import AccountMaterial

        frozen = session.get(
            BuildUnit, session.get(ExecutionStep, ids["CTA"][0]).unit_id
        )
        for i in range(18):
            asset = material(session, context, f"Moon-large-{i}.mp4", bc=draft.bc_id)
            session.add(
                AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id=draft.bc_id,
                    material_id=asset.id,
                    advertiser_id="account-A",
                    connection_id=frozen.connection_id,
                    video_id=f"large-vid-{i}",
                    image_id=f"large-cover-{i}",
                    status="available",
                    verified_at=datetime.now(UTC),
                )
            )
        identity = create_draft(session, context=context, **intent)
        task = prepare_draft(
            session, context=context, draft_id=identity, request_id=uuid4()
        )
        ready_links(session, context, task, intent)
        finish(session, context, task)
        preview = previews.generate_preview(
            session, context=context, draft_id=identity, expected_revision=1
        )
        drain(session, context, preview)
        submission_id = submissions.submit_preview(
            session, context=context, preview_id=preview, request_id=uuid4()
        ).submission_id
        while not submissions.expand_submission(
            session, context=context, submission_id=submission_id, limit=100
        ):
            pass
        ads = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.submission_id == submission_id, ExecutionStep.kind == "AD"
            )
            .order_by(ExecutionStep.id)
            .limit(501)
        ).all()
        assert len(ads) == 501
        for ad in ads:
            ad.status, ad.phase = "UNKNOWN", "DONE"
            session.add(ad)
    receipt = request(executable, submission_id, kind="RECONCILE")
    for revision, expected in enumerate([100, 200, 300, 400, 500, 501]):
        run(executable, receipt, revision)
        # Lost ACK replays the same generation, never another group of steps.
        run(executable, receipt, revision)
        with Session(db) as session:
            job = session.get(SubmissionRecovery, receipt.recovery_id)
            assert job.scanned_count == job.scheduled_count == expected
            assert job.state == ("COMPLETED" if expected == 501 else "RUNNING")
            dispatched = session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == submission_id,
                    col(ExecutionStep.dispatch_id).is_not(None),
                )
            ).all()
            assert len(dispatched) == expected and all(
                s.dispatch_revision == 1 for s in dispatched
            )


def test_expired_old_recovery_owner_cannot_publish_a_step(executable, monkeypatch):
    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    receipt = request(executable, submission_id)
    original = recovery._schedule

    def steal(session, **kwargs):
        # The job lock means another transaction cannot steal a live claim; this
        # forces the exact persisted boundary after the first page transaction.
        result = original(session, **kwargs)
        kwargs["job"].lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return result

    monkeypatch.setattr(recovery, "_schedule", steal)
    run(executable, receipt)
    with Session(db) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert job.state == "RUNNING" and job.scheduled_count == 1
        assert session.get(ExecutionStep, step_id).dispatch_revision == 1
    monkeypatch.setattr(recovery, "_schedule", original)
    run(executable, receipt)
    with Session(db) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert job.state == "COMPLETED" and job.scheduled_count == 1


def test_different_recovery_jobs_race_to_one_step_dispatch(executable):
    db, _, _ = executable
    submission_id, step_id = setup_failure(executable)
    first, second = (
        request(executable, submission_id),
        request(executable, submission_id),
    )
    barrier = Barrier(2)

    def worker(receipt):
        barrier.wait(timeout=10)
        run(executable, receipt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(worker, first), pool.submit(worker, second)
        a.result(timeout=15)
        b.result(timeout=15)
    with Session(db) as session:
        assert session.get(ExecutionStep, step_id).dispatch_revision == 1
        assert (
            sum(
                session.get(SubmissionRecovery, receipt.recovery_id).scheduled_count
                for receipt in (first, second)
            )
            == 1
        )


def test_crash_resumes_committed_cursor_without_replacing_published_steps(
    executable, monkeypatch
):
    db, _, ids = executable
    with Session(db) as session, session.begin():
        for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD"):
            for identity in ids[kind]:
                step = session.get(ExecutionStep, identity)
                step.status, step.phase = "UNKNOWN", "DONE"
                session.add(step)
                submission_id = step.submission_id
    receipt = request(executable, submission_id, kind="RECONCILE")
    original, count = recovery._schedule, 0

    def fail_second(session, **kwargs):
        nonlocal count
        count += 1
        result = original(session, **kwargs)
        if count == 2:
            raise RuntimeError("injected process loss after enqueue before commit")
        return result

    monkeypatch.setattr(recovery, "_schedule", fail_second)
    with pytest.raises(RuntimeError):
        run(executable, receipt)
    with Session(db) as session, session.begin():
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert job.scanned_count == job.scheduled_count == 1
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        session.add(job)
    monkeypatch.setattr(recovery, "_schedule", original)
    recovery.repair_recoveries(database_engine=db)
    run(executable, receipt)
    with Session(db) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert (
            job.state == "COMPLETED" and job.scanned_count == job.scheduled_count == 5
        )
        assert all(
            session.get(ExecutionStep, identity).dispatch_revision == 1
            for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD")
            for identity in ids[kind]
        )


def test_dependency_failed_child_is_only_retryable_after_actual_parent_and_own_materials(
    executable,
):
    db, context, ids = executable
    submission_id, group_id = setup_failure(
        executable, kind="ADGROUP", error_code="dependency_failed"
    )
    with Session(db) as session:
        assert (
            submissions.get_submission(
                session, context=context, submission_id=submission_id
            ).recovery.retryable_step_count
            == 0
        )
    with Session(db) as session, session.begin():
        parent = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        parent.status, parent.remote_id = "SUCCEEDED", "actual-campaign"
        session.add(parent)
        for identity in ids["MATERIAL"]:
            material = session.get(ExecutionStep, identity)
            material.status = "SUCCEEDED"
            session.add(material)
    receipt = request(executable, submission_id)
    run(executable, receipt)
    with Session(db) as session:
        assert session.get(ExecutionStep, group_id).status == "QUEUED"


def test_account_permission_revocation_changes_sql_counts_without_mutating_steps(
    executable,
):
    from app.modules.accounts.models import BCAccountAccess

    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    with Session(db) as session:
        assert (
            submissions.get_submission(
                session, context=context, submission_id=submission_id
            ).recovery.can_retry
            is True
        )
    with Session(db) as session, session.begin():
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id
            )
        ).one()
        grant.permission_state = "UNKNOWN"
        session.add(grant)
    with Session(db) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert not submissions.get_submission(
            session, context=context, submission_id=submission_id
        ).recovery.can_retry
        assert session.get(ExecutionStep, step_id).status == "FAILED"
    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request(executable, submission_id)
