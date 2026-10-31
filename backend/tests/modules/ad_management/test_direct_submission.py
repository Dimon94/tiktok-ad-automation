from uuid import uuid4

from sqlmodel import select

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.direct_submission import submit_direct_task
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.preparation import prepare_direct_chunk
from app.modules.ad_management.schemas import MutationSpec
from tests.modules.ad_management.conftest import _row, _set_selection


def test_direct_freezes_and_prepares_without_selection(session, management_env):
    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "direct-1")
    session.add(
        _row(ref, connection_id=route.connection_id, configuration={"budget": "100"})
    )
    _set_selection(selection, [ref])
    session.flush()
    key = uuid4()
    mutation = MutationSpec(field="budget", mode="increase_percent", value="10")
    result = submit_direct_task(session, context, bc.bc_id, selection.id, mutation, key)
    assert result.status == "PREPARING"
    assert not session.exec(select(ManagementPreview)).all()
    session.delete(selection)
    session.flush()
    assert (
        submit_direct_task(
            session, context, bc.bc_id, selection.id, mutation, key
        ).task_id
        == result.task_id
    )
    assert prepare_direct_chunk(session, result.task_id)
    item = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).one()
    assert item.final_value == "110.0"
    assert item.preview_id is None
    assert prepare_direct_chunk(session, result.task_id)
    assert (
        len(
            session.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == result.task_id
                )
            ).all()
        )
        == 1
    )


def test_direct_chunk_cancel_and_relative_values_are_not_recomputed(
    session, management_env, monkeypatch
):
    from app.modules.ad_management import preparation
    from app.modules.ad_management.actions import cancel_task

    monkeypatch.setattr(preparation, "PREPARE_CHUNK_SIZE", 1)
    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(context.tenant_id, account.advertiser_id, "campaign", f"chunk-{i}")
        for i in range(2)
    ]
    rows = [
        _row(r, connection_id=route.connection_id, configuration={"budget": "100"})
        for r in refs
    ]
    session.add_all(rows)
    _set_selection(selection, refs)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="budget", mode="add", value="10"),
        uuid4(),
    )
    assert not prepare_direct_chunk(session, result.task_id)
    rows[0].configuration = {"budget": "999"}
    session.add(rows[0])
    session.flush()
    cancel_task(session, context, result.task_id)
    assert prepare_direct_chunk(session, result.task_id)
    items = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).all()
    assert len(items) == 1 and items[0].final_value == "110"
    assert session.get(ManagementTask, result.task_id).status == "CANCELLED"


def test_direct_history_paginates_and_filters(session, management_env):
    from app.modules.ad_management.actions import get_task, list_tasks

    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(context.tenant_id, account.advertiser_id, "campaign", f"page-{i}")
        for i in range(4)
    ]
    session.add_all([_row(r, connection_id=route.connection_id) for r in refs])
    _set_selection(selection, refs)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    assert (
        list_tasks(
            session, context, bc.bc_id, cursor=None, limit=10, action="disable"
        ).total
        == 1
    )
    prepare_direct_chunk(session, result.task_id)
    public = get_task(session, context, bc.bc_id, result.task_id, limit=2)
    assert (
        len(public.items) == 2
        and public.next_items_offset == 2
        and public.items_total == 4
    )
    assert public.mutation.field == "status"


def test_direct_revoked_account_is_skipped_and_hidden(session, management_env):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ad_management.actions import list_tasks

    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "revoked")
    session.add(_row(ref, connection_id=route.connection_id))
    _set_selection(selection, [ref])
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    grant = session.get(
        BCAccountAccess,
        (context.tenant_id, bc.bc_id, account.advertiser_id, route.connection_id),
    )
    grant.authorized = False
    session.add(grant)
    session.flush()
    assert list_tasks(session, context, bc.bc_id, cursor=None, limit=10).total == 0
    prepare_direct_chunk(session, result.task_id)
    item = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).one()
    assert item.execution_result == "UNSUPPORTED"


def test_direct_10748_submission_does_not_expand_directory(session, management_env):
    from sqlalchemy import event

    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(context.tenant_id, account.advertiser_id, "campaign", f"large-{i}")
        for i in range(10748)
    ]
    _set_selection(selection, refs)
    session.flush()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _ctx, _many):
        statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        result = submit_direct_task(
            session,
            context,
            bc.bc_id,
            selection.id,
            MutationSpec(field="status", mode="set", value="DISABLE"),
            uuid4(),
        )
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    assert result.counts.selected == 10748
    assert len(statements) <= 25
    assert not any("FROM ad_object" in query for query in statements)
    task = session.get(ManagementTask, result.task_id)
    assert len(task.request["selection"]["refs"]) == 10748


def test_direct_preparation_queries_are_batched(session, management_env):
    from sqlalchemy import event

    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(context.tenant_id, account.advertiser_id, "campaign", f"batched-{i}")
        for i in range(200)
    ]
    session.add_all([_row(r, connection_id=route.connection_id) for r in refs])
    _set_selection(selection, refs)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _ctx, _many):
        if statement.lstrip().startswith("SELECT"):
            statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        assert prepare_direct_chunk(session, result.task_id)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    assert len(statements) < 30


def test_direct_material_chunks_never_toggle_ad(session, management_env, monkeypatch):
    from app.integrations.tiktok.contracts.ads import MaterialUseRef
    from app.modules.ad_management import preparation
    from app.modules.ads.models import AdMaterialReference

    monkeypatch.setattr(preparation, "PREPARE_CHUNK_SIZE", 1)
    context, bc, account, route, selection = management_env
    campaign = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "material-campaign"
    )
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "material-ad")
    session.add_all(
        [
            _row(campaign, connection_id=route.connection_id),
            _row(
                ad,
                parent=campaign,
                ad_type="SMART_PLUS",
                connection_id=route.connection_id,
            ),
        ]
    )
    uses = [
        MaterialUseRef(ad, f"video-{i}", f"material-{i}", "VIDEO") for i in range(2)
    ]
    for use in uses:
        session.add(
            AdMaterialReference(
                tenant_id=context.tenant_id,
                advertiser_id=account.advertiser_id,
                ad_remote_id=ad.remote_id,
                platform_material_id=use.platform_material_id,
                ad_material_id=use.ad_material_id,
                material_type="VIDEO",
                complete=True,
                operation_status="ENABLE",
                published_version=1,
            )
        )
    _set_selection(selection, [ad], uses)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    assert not prepare_direct_chunk(session, result.task_id)
    assert prepare_direct_chunk(session, result.task_id)
    items = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).all()
    assert len(items) == 2
    assert all(
        i.material_use and i.capability["operation"] == "set_material_status"
        for i in items
    )


def test_direct_bad_numeric_value_skips_only_one_member(session, management_env):
    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(
            context.tenant_id, account.advertiser_id, "campaign", f"bad-value-{i}"
        )
        for i in range(2)
    ]
    for i, ref in enumerate(refs):
        session.add(
            _row(
                ref,
                connection_id=route.connection_id,
                configuration={"budget": "bad" if i else "100"},
            )
        )
    _set_selection(selection, refs)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="budget", mode="add", value="10"),
        uuid4(),
    )
    prepare_direct_chunk(session, result.task_id)
    items = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).all()
    assert {i.execution_result for i in items} == {"PENDING", "UNSUPPORTED"}


def test_direct_retry_preserves_source_and_final_values(session, management_env):
    from app.modules.ad_management.actions import prepare_restore, retry_task

    context, bc, account, route, selection = management_env
    ref = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "retry-direct"
    )
    row = _row(ref, connection_id=route.connection_id, configuration={"budget": "100"})
    session.add(row)
    _set_selection(selection, [ref])
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="budget", mode="add", value="10"),
        uuid4(),
    )
    prepare_direct_chunk(session, result.task_id)
    item = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).one()
    item.execution_result = "NOT_SENT"
    session.add(item)
    session.flush()
    retried = retry_task(session, context, result.task_id, uuid4())
    copy = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == retried.task_id)
    ).one()
    assert (
        copy.source_kind == "DIRECT"
        and copy.final_value == "110"
        and copy.preview_id is None
    )
    session.delete(selection)
    row.configuration = {"budget": "110"}
    session.add(row)
    session.flush()
    restored = prepare_restore(session, context, result.task_id)
    assert restored.items[0].final_value == "100"


def test_direct_source_constraint_rejects_preview_item(session, management_env):
    import pytest
    from sqlalchemy.exc import IntegrityError

    context, bc, _, _, selection = management_env
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementTaskItem(
                tenant_id=context.tenant_id,
                task_id=result.task_id,
                source_kind="PREVIEW",
                preview_id=None,
                preview_item_id=None,
                ref={},
            )
        )
        session.flush()


def test_later_selected_parent_promotes_linked_hint(
    session, management_env, monkeypatch
):
    from app.modules.ad_management import preparation

    monkeypatch.setattr(preparation, "PREPARE_CHUNK_SIZE", 1)
    context, bc, account, route, selection = management_env
    parent = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "explicit-parent"
    )
    child = EntityRef(
        context.tenant_id, account.advertiser_id, "adgroup", "explicit-child"
    )
    session.add_all(
        [
            _row(parent, connection_id=route.connection_id, status="DISABLE"),
            _row(
                child,
                parent=parent,
                connection_id=route.connection_id,
                status="DISABLE",
            ),
        ]
    )
    _set_selection(selection, [child, parent])
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="ENABLE"),
        uuid4(),
    )
    assert not prepare_direct_chunk(session, result.task_id)
    assert prepare_direct_chunk(session, result.task_id)
    items = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).all()
    assert len(items) == 2 and all(i.execution_result == "PENDING" for i in items)
    assert session.get(ManagementTask, result.task_id).counts["targets"] == 2


def test_cancel_covers_retryable_not_sent_without_overwriting_claims_or_receipts(
    session, management_env
):
    from app.modules.ad_management.actions import cancel_task
    from app.modules.ad_management.models import ManagementRequestAttempt

    context, bc, account, route, selection = management_env
    refs = [
        EntityRef(context.tenant_id, account.advertiser_id, "campaign", f"cancel-{i}")
        for i in range(4)
    ]
    session.add_all([_row(ref, connection_id=route.connection_id) for ref in refs])
    _set_selection(selection, refs)
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    prepare_direct_chunk(session, result.task_id)
    items = session.exec(
        select(ManagementTaskItem)
        .where(ManagementTaskItem.task_id == result.task_id)
        .order_by(ManagementTaskItem.position)
    ).all()
    for item, state in zip(
        items, ["NOT_SENT", "NOT_SENT", "UNKNOWN", "ACCEPTED"], strict=True
    ):
        item.execution_result = state
        session.add(item)
    items[1].claim_token = uuid4()
    session.add(items[1])
    session.add(
        ManagementRequestAttempt(
            tenant_id=context.tenant_id,
            task_item_id=items[0].id,
            outcome="NOT_SENT",
            retryable=True,
            response={"reason": "admission_deferred"},
        )
    )
    session.flush()
    cancelled = cancel_task(session, context, result.task_id)
    assert cancelled.status == "CANCELLED"
    assert [item.execution_result for item in items] == [
        "CANCELLED",
        "NOT_SENT",
        "UNKNOWN",
        "ACCEPTED",
    ]
    assert items[0].delivery_status == "CANCELLED"
    assert prepare_direct_chunk(session, result.task_id)
    assert session.get(ManagementTask, result.task_id).status == "CANCELLED"


def test_preparation_watchdogs_coalesce_in_bounded_future_buckets():
    from datetime import UTC, datetime, timedelta

    from app.modules.ad_management.preparation_dispatch import (
        preparation_watchdog_entry,
    )

    task = ManagementTask(
        tenant_id=uuid4(),
        bc_id="test-bc",
        actor_id=uuid4(),
        idempotency_key=uuid4(),
        digest="a" * 64,
    )
    now = datetime(2026, 10, 31, 1, 0, 2, tzinfo=UTC)
    first = preparation_watchdog_entry(task, now=now)
    same = preparation_watchdog_entry(task, now=now + timedelta(seconds=10))
    later = preparation_watchdog_entry(task, now=now + timedelta(seconds=30))
    assert first == same
    assert later[1] != first[1]
    assert now + timedelta(seconds=120) <= first[3] <= now + timedelta(seconds=150)


def test_pause_skips_deleted_campaign_even_when_operation_status_is_enable(
    session, management_env
):
    context, bc, account, route, selection = management_env
    ref = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "deleted-campaign"
    )
    row = _row(ref, connection_id=route.connection_id)
    row.operation_status = "ENABLE"
    row.delivery_status = "CAMPAIGN_STATUS_DELETE"
    session.add(row)
    _set_selection(selection, [ref])
    session.flush()
    result = submit_direct_task(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
        uuid4(),
    )
    assert prepare_direct_chunk(session, result.task_id)
    item = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.task_id == result.task_id)
    ).one()
    assert item.execution_result == "NO_CHANGE" and item.reason == "already_deleted"
