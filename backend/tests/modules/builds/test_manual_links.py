from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.catalog import draft_summary, inputs_page
from app.modules.builds.drafts import create_draft, prepare_draft
from app.modules.builds.models import DraftDrama, DraftPreparation
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderConnection,
)
from tests.modules.builds.test_drafts import account

URL = "https://www.tiktok.com/minis/example?minis_id=mini-test&channel=a%2Bb&x=1&x=2"


def test_partial_title_edit_cannot_reuse_old_line_number_mapping(
    session, context, intent
):
    from app.modules.builds.drafts import update_draft

    intent.update(drama_lines=["First", "Second"], manual_links=[manual()])
    draft_id = create_draft(session, context=context, **intent)
    with pytest.raises(DomainError) as error:
        update_draft(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=1,
            drama_lines=["Second", "First"],
        )
    assert error.value.code == "manual_link_mapping_required"


def test_different_attribution_names_are_not_silently_deduplicated(
    session, context, intent
):
    account(session, context)
    intent.update(
        drama_lines=["Same", "Same"],
        manual_links=[
            manual(protected_base="prefix-a"),
            manual(2, protected_base="prefix-b"),
        ],
    )
    draft_id = create_draft(session, context=context, **intent)
    with pytest.raises(DomainError) as error:
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4())
    assert error.value.code == "manual_link_conflict"


@pytest.mark.parametrize("numeric_input", [False, True])
def test_automatic_discovery_insert_race_does_not_fail_manual_preparation(
    isolated_strategy_database,
    numeric_input,
):
    from sqlalchemy import event
    from sqlalchemy.dialects.postgresql import insert
    from sqlmodel import Session

    from app.modules.providers.models import ProviderDrama
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        intent = create_intent(session, context)
        account(session, context)
        intent.update(
            drama_lines=["Manual"],
            manual_links=[
                manual(external_drama_id="31091" if numeric_input else "remote-123")
            ],
        )
        draft_id = create_draft(session, context=context, **intent)
        session.commit()
    raced = False

    def competing_insert(
        _conn, _cursor, statement, _parameters, _execution_context, _executemany
    ):
        nonlocal raced
        if raced or not statement.startswith("INSERT INTO provider_drama"):
            return
        raced = True
        # 在手动 INSERT 之前由另一事务提交自动发现结果，真实复现唯一键竞争。
        with Session(engine) as other:
            other.exec(
                insert(ProviderDrama)
                .values(
                    id=uuid4(),
                    tenant_id=context.tenant_id,
                    connection_id=intent["provider_connection_id"],
                    application_id=intent["application_id"],
                    external_drama_id="remote-123",
                    display_drama_id="31091" if numeric_input else None,
                    title="Remote title",
                )
                .on_conflict_do_nothing()
            )
            other.commit()

    event.listen(engine, "before_cursor_execute", competing_insert)
    try:
        with Session(engine) as session:
            prepare_draft(
                session, context=context, draft_id=draft_id, request_id=uuid4()
            )
            drama = session.exec(
                select(DraftDrama).where(DraftDrama.draft_id == draft_id)
            ).one()
            assert session.get(PromotionLink, drama.link_id).url == URL
            assert session.get(ProviderDrama, drama.drama_id).title == "Remote title"
            assert (
                len(
                    session.exec(
                        select(ProviderDrama).where(
                            ProviderDrama.connection_id
                            == intent["provider_connection_id"]
                        )
                    ).all()
                )
                == 1
            )
            assert raced
    finally:
        event.remove(engine, "before_cursor_execute", competing_insert)


def manual(line=1, **kw):
    return {"line_no": line, "url": URL, **kw}


@pytest.mark.parametrize("media_kind", ["VIDEO", "CAROUSEL"])
def test_manual_override_does_not_wait_for_reused_unknown_provider(
    session, context, intent, media_kind
):
    from app.modules.builds.drafts import update_draft
    from app.modules.builds.models import BuildDraft
    from tests.modules.builds.test_drafts import finish, ready_links

    account(session, context)
    intent.update(drama_lines=["Moon", "Sun"])
    draft_id = create_draft(session, context=context, media_kind=media_kind, **intent)
    original = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, original, intent)
    finish(session, context, original)
    provider_id = session.get(DraftPreparation, original).provider_task_id
    item = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == provider_id,
            LinkPreparationItem.line_no == 1,
        )
    ).one()
    item.status = "result_unknown"
    item.resolved = {
        **item.resolved,
        "status": "result_unknown",
        "error_code": "provider_result_unknown",
        "_work": {**item.resolved.get("_work", {}), "stage": "recover_create"},
    }
    session.add(item)
    session.flush()
    checkpoint = dict(item.resolved)
    draft = session.get(BuildDraft, draft_id)
    update_draft(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=draft.revision,
        manual_links=[manual()],
    )
    renewed = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert session.get(DraftPreparation, renewed).provider_task_id == provider_id
    finish(session, context, renewed)
    assert session.get(BuildDraft, draft_id).status == "READY"
    row = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items[
        0
    ]
    assert row.preparation.link_status == "ready"
    assert item.status == "result_unknown" and item.resolved == checkpoint


def test_other_provider_prepares_without_remote_connection(session, context, intent):
    account(session, context)
    intent.update(
        provider_connection_id=None,
        application_id=None,
        custom_provider_name="新版权方",
        drama_lines=["Moon", "No Link"],
        manual_links=[manual()],
    )
    request = uuid4()
    draft = create_draft(session, context=context, request_id=request, **intent)
    assert create_draft(session, context=context, request_id=request, **intent) == draft
    prep_id = prepare_draft(
        session, context=context, draft_id=draft, request_id=uuid4()
    )
    prep = session.get(DraftPreparation, prep_id)
    assert prep.provider_task_id is None
    rows = inputs_page(session, context=context, draft_id=draft, kind="drama").items
    assert rows[0].preparation.link_status == "ready"
    assert rows[1].preparation.reason_code == "manual_link_required"
    drama = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).one()
    link = session.get(PromotionLink, drama.link_id)
    assert link.url == URL
    assert link.source == "manual"
    assert link.attribution["recorded_by"] == str(context.actor_id)
    assert (
        draft_summary(session, context=context, draft_id=draft).custom_provider_name
        == "新版权方"
    )


def test_mixed_inputs_send_only_missing_links_and_keep_original_line_numbers(
    session, context, intent
):
    account(session, context)
    intent.update(
        drama_lines=["Manual", "Automatic", "Another"],
        manual_links=[manual(), manual(3)],
    )
    draft = create_draft(session, context=context, **intent)
    prep = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft, request_id=uuid4()),
    )
    rows = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == prep.provider_task_id
        )
    ).all()
    assert [(row.line_no, row.raw_input) for row in rows] == [(2, "Automatic")]
    assert (
        len(session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).all())
        == 2
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/a",
        "javascript:alert(1)",
        "https://www.tiktok.com@evil.example/minis/a",
        "https://www.tiktok.com/minis/a\n",
        "https://www.tiktok.com/video/a",
    ],
)
def test_manual_url_rejects_non_minis_or_ambiguous_input(session, context, intent, url):
    with pytest.raises(DomainError) as error:
        create_draft(session, context=context, **intent, manual_links=[manual(url=url)])
    assert error.value.code == "manual_link_invalid"


def test_wangyan_manual_link_requires_attribution_name(session, context, intent):
    connection = session.get(ProviderConnection, intent["provider_connection_id"])
    connection.kind = "wangyan"
    session.flush()
    with pytest.raises(DomainError) as error:
        create_draft(session, context=context, **intent, manual_links=[manual()])
    assert error.value.code == "manual_attribution_required"


def test_manual_supplement_reuses_pending_provider_work_for_other_rows(
    session, context, intent
):
    from app.modules.builds.manual_links import save_manual_link
    from app.modules.builds.models import BuildDraft

    account(session, context)
    intent.update(drama_lines=["First", "Second"])
    draft_id = create_draft(session, context=context, **intent)
    old = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    draft = session.get(BuildDraft, draft_id)
    draft.status = "BLOCKED"
    old.status = "BLOCKED"
    session.flush()
    row = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items[
        0
    ]
    request = uuid4()
    arguments = {
        "context": context,
        "draft_id": draft_id,
        "input_id": row.id,
        "expected_revision": 1,
        "request_id": request,
        "link": manual(),
    }
    assert save_manual_link(session, **arguments) == 2
    assert save_manual_link(session, **arguments) == 2
    new = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    assert new.provider_task_id == old.provider_task_id
    rows = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items
    assert rows[0].preparation.link_status == "ready"
    assert rows[0].preparation.candidates == []


@pytest.mark.parametrize(
    ("status", "error_code", "rerun"),
    [
        ("failed", "provider_unavailable", True),
        ("needs_resolution", "drama_not_found", True),
        ("needs_resolution", "drama_ambiguous", False),
        ("result_unknown", "provider_result_unknown", False),
        ("ready", None, False),
    ],
)
def test_reprepare_reruns_provider_links_after_a_failed_link(
    session, context, intent, status, error_code, rerun
):
    """同剧单重新准备：上次有剧取链失败时必须重新取链，不能复用失败结果。"""
    from app.modules.builds.models import BuildDraft

    account(session, context)
    intent.update(drama_lines=["First", "Second"])
    draft_id = create_draft(session, context=context, **intent)
    old = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    items = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == old.provider_task_id
        )
    ).all()
    items[0].status = status
    items[0].resolved = {**items[0].resolved, "error_code": error_code}
    draft = session.get(BuildDraft, draft_id)
    draft.status = "BLOCKED"
    old.status = "BLOCKED"
    session.add_all([items[0], draft, old])
    session.flush()
    new = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    assert new.provider_task_id is not None
    assert (new.provider_task_id != old.provider_task_id) == rerun


@pytest.mark.parametrize(
    "suffix", ["?minis_id=a&minis_id=b", "?minis_id=", "?minis_id=a%0Ab"]
)
def test_manual_link_rejects_ambiguous_mini_identity(session, context, intent, suffix):
    with pytest.raises(DomainError) as error:
        create_draft(
            session,
            context=context,
            **intent,
            manual_links=[manual(url="https://www.tiktok.com/minis/abc" + suffix)],
        )
    assert error.value.code == "manual_link_invalid"


def test_supplement_does_not_cross_tenants_or_overwrite_a_new_revision(
    session, context, other_context, intent
):
    from app.modules.builds.manual_links import save_manual_link

    draft_id = create_draft(session, context=context, **intent)
    row = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items[
        0
    ]
    kwargs = {
        "draft_id": draft_id,
        "input_id": row.id,
        "expected_revision": 1,
        "request_id": uuid4(),
        "link": manual(),
    }
    with pytest.raises(DomainError):
        save_manual_link(session, context=other_context, **kwargs)
    assert save_manual_link(session, context=context, **kwargs) == 2
    with pytest.raises(DomainError) as error:
        save_manual_link(
            session,
            context=context,
            **(kwargs | {"request_id": uuid4(), "link": manual(url=URL + "&new=1")}),
        )
    assert error.value.code == "draft_revision_conflict"


def test_manual_link_uses_real_bc_route_and_freezes_long_provider_name(
    session, context, intent, monkeypatch
):
    from app.modules.builds import previews
    from app.modules.builds.drafts import _preparation_route
    from app.modules.builds.manual_links import save_manual_link
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.preview_models import PreviewDrama
    from app.modules.builds.scene import _scope

    account(session, context)
    name = "某个版权方" * 12
    intent.update(
        provider_connection_id=None,
        application_id=None,
        custom_provider_name=name,
        drama_lines=["Manual"],
        manual_links=[manual(external_drama_id="32827")],
    )
    draft_id = create_draft(session, context=context, **intent)
    prep = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    draft = session.get(BuildDraft, draft_id)
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).one()
    route = _preparation_route(session, context, draft, prep)
    scope = _scope(
        session,
        context=context,
        bc_id="bc-draft",
        advertiser_id="account-A",
        link_id=drama.link_id,
        route=route,
    )
    assert scope["minis_id"] == "mini-test"
    with pytest.raises(DomainError):
        _scope(
            session,
            context=context,
            bc_id="different-bc",
            advertiser_id="account-A",
            link_id=drama.link_id,
            route=route,
        )
    # This route/name test uses synthetic identity and targeting evidence.
    from app.modules.builds import identity_selection
    from tests.modules.builds.test_previews import seed_targeting_directory

    monkeypatch.setattr(
        identity_selection,
        "require_preview_identity",
        lambda *a, **kw: {
            "identity_id": "fixture-identity",
            "identity_type": "TT_USER",
        },
    )
    from tests.modules.builds.test_drafts import finish

    finish(session, context, prep.id)
    seed_targeting_directory(session, context, ["account-A"], minis_id="mini-test")
    # 无素材时准备无需外部调用；直接验证已有本地事实进入冻结预览的过程。
    draft.status = "READY"
    prep.status = "READY"
    session.flush()
    preview_id = previews.generate_preview(
        session, context=context, draft_id=draft_id, expected_revision=1
    )
    for _ in range(20):
        if previews.continue_preview(session, context=context, preview_id=preview_id):
            break
    frozen = session.exec(
        select(PreviewDrama).where(PreviewDrama.preview_id == preview_id)
    ).one()
    assert frozen.provider_pinyin == name
    assert frozen.external_drama_id == "32827"
    assert frozen.display_drama_id == "32827"
    assert frozen.url == URL
    old_summary = previews.get_preview_summary(
        session, context=context, preview_id=preview_id
    )
    row = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items[
        0
    ]
    save_manual_link(
        session,
        context=context,
        draft_id=draft_id,
        input_id=row.id,
        expected_revision=1,
        request_id=uuid4(),
        link=manual(url=URL + "&updated=1"),
    )
    session.refresh(frozen)
    assert frozen.url == URL
    assert (
        previews.get_preview_summary(
            session, context=context, preview_id=preview_id
        ).content_digest
        == old_summary.content_digest
    )


def test_http_manual_scope_and_link_validation(client, session, context, intent):
    from app.modules.tenants.models import TenantMembership
    from tests.modules.strategies.test_api import headers

    base = f"/api/tenants/{context.tenant_id}/build-drafts"
    payload = {
        **intent,
        "strategy_version_id": str(intent["strategy_version_id"]),
        "request_id": str(uuid4()),
        "provider_connection_id": None,
        "application_id": None,
        "custom_provider_name": "新版权方",
        "drama_lines": ["Manual"],
    }
    response = client.post(base, headers=headers(context), json=payload)
    assert response.status_code == 201
    draft_id = response.json()["draft_id"]
    rows = client.get(
        f"{base}/{draft_id}/inputs?kind=drama", headers=headers(context)
    ).json()["items"]
    url = f"{base}/{draft_id}/inputs/{rows[0]['id']}/manual-link"
    body = {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "link": manual(url="https://wrong.example/a"),
    }
    response = client.put(url, headers=headers(context), json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "manual_link_invalid"
    body["link"] = manual()
    assert client.put(url, headers=headers(context), json=body).json()["revision"] == 2
    assert client.put(url, headers=headers(context), json=body).json()["revision"] == 2
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.flush()
    assert client.put(url, headers=headers(context), json=body).status_code == 403


def test_reverse_order_manual_drafts_prepare_without_deadlock(
    isolated_strategy_database,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError

    from sqlalchemy import event
    from sqlmodel import Session

    from app.modules.providers.models import ProviderDrama
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        intent = create_intent(session, context)
        account(session, context)
        first = create_draft(
            session,
            context=context,
            **(
                intent
                | {
                    "drama_lines": ["A", "B"],
                    "manual_links": [
                        manual(external_drama_id="a"),
                        manual(2, external_drama_id="b"),
                    ],
                }
            ),
        )
        second = create_draft(
            session,
            context=context,
            **(
                intent
                | {
                    "drama_lines": ["B", "A"],
                    "manual_links": [
                        manual(external_drama_id="b"),
                        manual(2, external_drama_id="a"),
                    ],
                }
            ),
        )
        for identity in ["a", "b"]:
            session.add(
                ProviderDrama(
                    tenant_id=context.tenant_id,
                    connection_id=intent["provider_connection_id"],
                    application_id=intent["application_id"],
                    external_drama_id=identity,
                    title=identity.upper(),
                )
            )
        session.commit()
    start, first_reads = Barrier(2), Barrier(2)
    seen = set()

    def align_reads(conn, _cursor, statement, _parameters, _context, _many):
        if "FROM provider_drama" not in statement or id(conn) in seen:
            return
        seen.add(id(conn))
        # 尽量对齐两个首次读取。合法的范围串行处理不需要等到另一个事务进入。
        try:
            first_reads.wait(timeout=0.2)
        except BrokenBarrierError:
            pass

    def prepare(identity):
        with Session(engine) as session:
            start.wait(timeout=5)
            result = prepare_draft(
                session, context=context, draft_id=identity, request_id=uuid4()
            )
            session.commit()
            return result

    event.listen(engine, "after_cursor_execute", align_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(prepare, identity) for identity in [first, second]
            ]
            assert all(future.result(timeout=10) for future in futures)
    finally:
        event.remove(engine, "after_cursor_execute", align_reads)


def test_missing_provider_still_returns_scoped_domain_error(session, context, intent):
    intent["provider_connection_id"] = uuid4()
    with pytest.raises(DomainError) as error:
        create_draft(session, context=context, **intent)
    assert error.value.code == "resource_not_found"


def test_jiashu_manual_link_rejects_another_drama_vid():
    from app.modules.builds.manual_links import validate_manual_links

    with pytest.raises(DomainError) as caught:
        validate_manual_links(
            [
                {
                    "line_no": 1,
                    "external_drama_id": "201222",
                    "url": "https://www.tiktok.com/minis/example?vid=201225",
                    "protected_base": "jiashu-example",
                    "material_identifier": "201222",
                }
            ],
            ["example"],
            "jiashu",
        )
    assert caught.value.code == "manual_link_drama_mismatch"
