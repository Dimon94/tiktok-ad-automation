"""Committed provider workflow against actual HTTP adapters, never live services."""

# ruff: noqa: F811
import json
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.modules.providers.link_steps import run_link_item
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderEffect,
)
from app.modules.providers.service import prepare_links
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)


def remote_link(identity, *, episode=1, name="historical", url=True):
    return {
        "id": identity,
        "app": "wy-app",
        "drama_id": "opaque-drama",
        "drama_int_id": 73,
        "chapter_index": episode,
        "promote_platform": "tiktok",
        "promote_name": name,
        "tt_minis_link": f"https://www.tiktok.com/t/wy-{identity}" if url else None,
    }


@dataclass
class Remote:
    title: str = "Moon"
    rows: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    omit_id: bool = False
    lose_reply: bool = False
    expose_created: bool = True
    duplicate_created: bool = False
    reject: bool = False
    after_post: object = None
    session: object = None

    def __call__(self, request):
        assert not self.session.in_transaction(), "database transaction spans HTTP"
        assert request.headers["cookie"] == "x-ds-admin-token=synthetic-wangyan"
        query = dict(request.url.params)
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, query, payload))
        if request.url.path.endswith("/drama/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [{"id": "opaque-drama", "title": self.title, "lang": "en"}]
                    if query["page"] == "1"
                    else [],
                },
            )
        if request.url.path.endswith("/link/list"):
            assert query["app"] == "wy-app" and query["start"] == "1970-01-01"
            rows = self.rows
            if "id" in query:
                rows = [r for r in rows if str(r["id"]) == query["id"]]
            else:
                assert query["drama_id"] == "opaque-drama"
            offset = (int(query["page"]) - 1) * 20
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": rows[offset : offset + 20],
                    "total": len(rows),
                },
            )
        assert request.url.path.endswith("/link/create") and request.method == "POST"
        assert payload["drama_id"] == "opaque-drama"
        assert payload["app"] == "wy-app" and payload["promote_platform"] == "tiktok"
        assert payload["chapter_index"] == 1 and "promote_name" not in payload
        if self.reject:
            return httpx.Response(200, json={"code": 400, "data": None})
        # 本次请求的远端行为在 after_post 运行前就已确定；after_post 只影响后续请求。
        lose, expose = self.lose_reply, self.expose_created
        row = remote_link(901)
        if expose:
            self.rows.append(row)
            if self.duplicate_created:
                self.rows.append({**row, "id": 902})
        if self.after_post:
            self.after_post()
        if lose:
            raise httpx.ReadTimeout(
                "synthetic committed response lost", request=request
            )
        return httpx.Response(
            200, json={"code": 0, "data": {} if self.omit_id else {"id": 901}}
        )


@pytest.fixture
def workflow(isolated_strategy_database, monkeypatch):
    db, context, _ = isolated_strategy_database
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    verification = uuid4()
    with Session(db) as session, session.begin():
        connection = ProviderConnection(
            tenant_id=context.tenant_id,
            kind="wangyan",
            display_name="Synthetic",
            status="active",
            credential_version=1,
            verification_token=verification,
            encrypted_credentials=encrypt_credentials(
                tenant_id=context.tenant_id, value={"token": "synthetic-wangyan"}
            ),
        )
        session.add(connection)
        session.flush()
        session.add(
            ProviderApplication(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                external_id="wy-app",
                name="Synthetic",
                tiktok_minis_id="synthetic-minis",
                channel_config={"is_tt": True, "verification_token": str(verification)},
            )
        )
        session.flush()
        prep = prepare_links(
            session,
            context=context,
            connection_id=connection.id,
            application_id="wy-app",
            lines=["Moon"],
            config={"episode": 1},
            request_id=uuid4(),
        )
        item = session.exec(
            select(LinkPreparationItem).where(
                LinkPreparationItem.preparation_id == prep
            )
        ).one()
        identity, connection_id = item.id, connection.id
    return db, context, identity, connection_id, Remote()


def advance(env):
    db, context, identity, _, remote = env
    with Session(db) as session:
        remote.session = session
        before = len(remote.calls)
        run_link_item(
            session,
            context=context,
            item_id=identity,
            transport=httpx.MockTransport(remote),
        )
        assert len(remote.calls) - before <= 1
    with Session(db) as session:
        return session.get(LinkPreparationItem, identity)


def finish(env, limit=30):
    for _ in range(limit):
        item = advance(env)
        if item.status in {"ready", "failed", "config_conflict", "blocked_auth"}:
            return item
    return item


def writes(env):
    return [r for r in env[-1].calls if r[0] == "POST"]


@pytest.mark.parametrize("title", ["Moon", "\t Moon\t\r\n"])
def test_reuses_minimum_valid_remote_id_after_all_history_pages(workflow, title):
    workflow[-1].title = title
    workflow[-1].rows = [remote_link(i, episode=2) for i in range(100, 120)] + [
        remote_link(27),
        remote_link(8),
    ]
    item = finish(workflow)
    assert item.status == "ready", item.resolved
    with Session(workflow[0]) as session:
        link = session.exec(select(PromotionLink)).one()
        assert link.remote_id == "8" and link.protected_base == "{b73/s8/c1}-Moon"
        assert link.url == "https://www.tiktok.com/t/wy-8"
    calls = workflow[-1].calls
    histories = [r for r in calls if r[1].endswith("/link/list") and "id" not in r[2]]
    assert [r[2]["page"] for r in histories] == ["1", "2"]
    assert calls[-1][2]["id"] == "8" and not writes(workflow)


@pytest.mark.parametrize("omit_id", [False, True])
def test_complete_empty_history_creates_once_then_reads_actual_id(workflow, omit_id):
    workflow[-1].omit_id = omit_id
    item = finish(workflow)
    assert item.status == "ready", item.resolved
    assert len(writes(workflow)) == 1
    with Session(workflow[0]) as session:
        link = session.exec(select(PromotionLink)).one()
        assert link.remote_id == "901" and link.protected_base == "{b73/s901/c1}-Moon"
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.remote_id == "901" and effect.status == "succeeded"
    assert workflow[-1].calls[-1][2]["id"] == "901"
    for _ in range(3):
        advance(workflow)
    assert len(writes(workflow)) == 1


@pytest.mark.parametrize("result", ["unique", "ambiguous"])
def test_unknown_post_adopts_single_new_link_or_stops(workflow, result):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.duplicate_created = result == "ambiguous"
    item = finish(workflow, limit=12)
    assert len(writes(workflow)) == 1
    assert item.status == ("ready" if result == "unique" else "result_unknown"), (
        item.resolved
    )
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert (effect.remote_id == "901") == (result == "unique")
        assert effect.status != "confirmed_absent"
    if result == "ambiguous":
        assert item.resolved["_work"]["stage"] == "manual_review"
        calls = list(remote.calls)
        checkpoint = item.resolved
        for _ in range(3):
            repeated = advance(workflow)
            assert repeated.status == "result_unknown"
            assert repeated.resolved == checkpoint
        assert remote.calls == calls


def test_unknown_post_with_no_new_link_recreates_once(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.expose_created = False

    def recover_network():
        # 第一次请求确实未落地；之后网络恢复，第二次建链正常返回。
        remote.lose_reply = False
        remote.expose_created = True
        remote.after_post = None

    remote.after_post = recover_network
    item = finish(workflow, limit=20)
    assert item.status == "ready", item.resolved
    assert len(writes(workflow)) == 2
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.status == "succeeded" and effect.remote_id == "901"
        link = session.exec(select(PromotionLink)).one()
        assert link.remote_id == "901"


def test_unknown_post_with_incomplete_recovery_scan_never_recreates(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.expose_created = False
    while not writes(workflow):
        advance(workflow)
    remote.rows = [remote_link(i, episode=2) for i in range(100, 121)]
    remote.rows[-1] = remote_link(100, episode=2)  # 分页重复，扫描不完整
    item = finish(workflow, limit=10)
    assert item.status == "result_unknown"
    assert len(writes(workflow)) == 1


def create_effect(env):
    with Session(env[0]) as session:
        return session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()


def expire_create_execution(env):
    from datetime import UTC, datetime, timedelta

    with Session(env[0]) as session, session.begin():
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        execution = {
            **effect.result["_execution"],
            "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        effect.result = {**effect.result, "_execution": execution}
        session.add(effect)


def test_crashed_sender_recreates_only_after_its_execution_window(workflow):
    remote = workflow[-1]
    remote.expose_created = False

    def worker_exit():
        remote.after_post = None
        remote.expose_created = True
        raise SystemExit("synthetic worker terminated before receiving response")

    remote.after_post = worker_exit
    with pytest.raises(SystemExit):
        finish(workflow)
    expire_completed_worker_claim(workflow)
    # 原发送方的执行期限未过：恢复扫描为 0 条也不能判定未落地。
    item = finish(workflow, limit=6)
    assert item.status == "result_unknown", item.resolved
    assert len(writes(workflow)) == 1 and create_effect(workflow).status == "sending"
    expire_create_execution(workflow)
    item = finish(workflow, limit=12)
    assert item.status == "ready", item.resolved
    assert len(writes(workflow)) == 2
    effect = create_effect(workflow)
    assert effect.status == "succeeded" and effect.remote_id == "901"


def test_late_receipt_after_confirmed_absent_blocks_recreate(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.expose_created = False
    for _ in range(12):
        advance(workflow)
        if writes(workflow) and create_effect(workflow).status == "confirmed_absent":
            break
    effect = create_effect(workflow)
    assert effect.status == "confirmed_absent" and len(writes(workflow)) == 1
    with Session(workflow[0]) as session, session.begin():
        session.add(
            ProviderEffect(
                tenant_id=effect.tenant_id,
                remote_scope_key=effect.remote_scope_key,
                step="wy_receipt",
                request_digest="synthetic-late-receipt",
                status="succeeded",
                remote_id="901",
                result={
                    "effect_id": str(effect.id),
                    "attempt": str(effect.attempt_token),
                    "remote_id": "901",
                },
            )
        )
    item = finish(workflow, limit=6)
    assert item.status == "result_unknown", item.resolved
    assert len(writes(workflow)) == 1


def test_accepted_create_without_visible_link_never_recreates(workflow):
    # 网眼已明确受理但列表查不到：不是“请求未落地”，只能人工核实。
    remote = workflow[-1]
    remote.omit_id = True
    remote.expose_created = False
    item = finish(workflow, limit=10)
    assert item.status == "result_unknown", item.resolved
    assert len(writes(workflow)) == 1


def set_role(env, role):
    from app.modules.tenants.models import TenantMembership

    with Session(env[0]) as session, session.begin():
        member = session.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == env[1].tenant_id,
                TenantMembership.user_id == env[1].actor_id,
            )
        ).one()
        member.role = role
        session.add(member)


def test_known_receipt_survives_revocation_and_resumes_only_read(workflow):
    remote = workflow[-1]
    remote.after_post = lambda: set_role(workflow, "viewer")
    item = finish(workflow, limit=6)
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.remote_id == "901"
        assert (
            session.exec(
                select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
            )
            .one()
            .remote_id
            == "901"
        )
    assert item.status == "result_unknown", item.resolved
    assert item.resolved["error_code"] == "action_forbidden"
    count = len(remote.calls)
    advance(workflow)
    assert len(remote.calls) == count
    set_role(workflow, "operator")
    assert finish(workflow).status == "ready"
    assert len(writes(workflow)) == 1


@pytest.mark.parametrize("fault", ["duplicate", "total_changed", "wrong_scope"])
def test_incomplete_history_never_authorizes_create(workflow, fault):
    remote = workflow[-1]
    remote.rows = [remote_link(i, episode=2) for i in range(100, 121)]
    if fault == "duplicate":
        remote.rows[-1] = remote_link(100, episode=2)
    if fault == "wrong_scope":
        remote.rows[-1]["drama_id"] = "another-opaque-drama"
    for _ in range(10):
        item = advance(workflow)
        scan = item.resolved.get("_work", {}).get("wy_scan", {})
        if fault == "total_changed" and scan.get("pages") == 1:
            remote.rows.append(remote_link(122, episode=2))
        if item.status == "failed":
            break
    assert (
        item.status == "failed" and item.resolved["error_code"] == "lookup_incomplete"
    )
    assert not writes(workflow)


def test_duplicate_worker_during_real_post_cannot_send_again(workflow):
    from concurrent.futures import ThreadPoolExecutor

    remote = workflow[-1]

    def duplicate():
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(advance, workflow).result(timeout=10).status == "pending"

    remote.after_post = duplicate
    assert finish(workflow).status == "ready"
    assert len(writes(workflow)) == 1


def expire_completed_worker_claim(workflow):
    from datetime import UTC, datetime, timedelta

    with Session(workflow[0]) as session, session.begin():
        item = session.get(LinkPreparationItem, workflow[2])
        state = dict(item.resolved)
        work = dict(state["_work"])
        if "claim_token" in work:
            work["claim_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        item.resolved = {**state, "_work": work}
        session.add(item)


def test_late_receipt_preserves_new_owner_checkpoint(workflow):
    remote = workflow[-1]
    new_owner = str(uuid4())

    def change_owner():
        with Session(workflow[0]) as session, session.begin():
            item = session.get(LinkPreparationItem, workflow[2])
            state = dict(item.resolved)
            item.resolved = {
                **state,
                "_work": {**state["_work"], "claim_token": new_owner},
            }
            session.add(item)

    remote.after_post = change_owner
    for _ in range(8):
        item = advance(workflow)
        if writes(workflow):
            break
    assert item.resolved["_work"]["claim_token"] == new_owner
    assert item.resolved["_work"]["stage"] == "create"
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.remote_id == "901" and effect.status == "result_unknown"
        assert (
            session.exec(
                select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
            )
            .one()
            .remote_id
            == "901"
        )
    expire_completed_worker_claim(workflow)
    assert finish(workflow).status == "ready"
    assert len(writes(workflow)) == 1


def test_receipt_survives_actual_pg_transaction_failure(workflow, monkeypatch):
    from sqlalchemy import text

    remote = workflow[-1]
    original_commit = Session.commit
    failures = []

    def commit(session):
        if session is remote.session and writes(workflow) and not failures:
            failures.append(True)
            # An actual failed PostgreSQL transaction, followed by receipt rescue.
            session.exec(text("SELECT * FROM acceptance_missing_receipt_table"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", commit)
    assert finish(workflow).status == "ready"
    assert failures == [True] and len(writes(workflow)) == 1
    with Session(workflow[0]) as session:
        assert (
            session.exec(
                select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
            )
            .one()
            .remote_id
            == "901"
        )


def test_client_cleanup_systemexit_cannot_drop_known_receipt(workflow, monkeypatch):
    original_exit = httpx.Client.__exit__
    raised = []

    def cleanup(client, *args):
        original_exit(client, *args)
        if writes(workflow) and not raised:
            raised.append(True)
            raise SystemExit("synthetic terminated worker cleanup")

    monkeypatch.setattr(httpx.Client, "__exit__", cleanup)
    with pytest.raises(SystemExit):
        finish(workflow)
    with Session(workflow[0]) as session:
        assert (
            session.exec(
                select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
            )
            .one()
            .remote_id
            == "901"
        )
    expire_completed_worker_claim(workflow)
    assert finish(workflow).status == "ready" and len(writes(workflow)) == 1


def test_unknown_scan_rejects_forged_effect_nonce_before_get(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    while not writes(workflow):
        advance(workflow)
    with Session(workflow[0]) as session, session.begin():
        item = session.get(LinkPreparationItem, workflow[2])
        state = dict(item.resolved)
        item.resolved = {
            **state,
            "_work": {**state["_work"], "uncertain_attempt_token": str(uuid4())},
        }
        session.add(item)
    calls = len(remote.calls)
    item = advance(workflow)
    assert (
        item.status == "result_unknown"
        and item.resolved["error_code"] == "provider_state_invalid"
    )
    assert len(remote.calls) == calls


def test_known_receipt_checkpoint_failure_remains_read_recoverable(
    workflow, monkeypatch
):
    from sqlalchemy import text

    from app.modules.providers import wangyan_steps

    original = wangyan_steps._checkpoint
    failures = []

    def checkpoint(session, context, item_id, token, work):
        if (
            work.get("remote_id") == "901"
            and work["stage"] == "verify"
            and not failures
        ):
            failures.append(True)
            session.exec(text("SELECT * FROM acceptance_missing_checkpoint_table"))
        return original(session, context, item_id, token, work)

    monkeypatch.setattr(wangyan_steps, "_checkpoint", checkpoint)
    while not writes(workflow):
        item = advance(workflow)
    assert item.status == "result_unknown", item.resolved
    assert finish(workflow).status == "ready"
    assert failures == [True] and len(writes(workflow)) == 1


def test_known_remote_config_conflict_is_reported_without_recreate(workflow):
    while not writes(workflow):
        advance(workflow)
    workflow[-1].rows[0]["chapter_index"] = 2
    item = advance(workflow)
    assert item.status == "config_conflict", item.resolved
    assert item.resolved["error_code"] == "config_conflict"
    calls = len(workflow[-1].calls)
    advance(workflow)
    assert len(workflow[-1].calls) == calls and len(writes(workflow)) == 1
    with Session(workflow[0]) as session:
        assert (
            session.exec(
                select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
            )
            .one()
            .remote_id
            == "901"
        )


def test_definite_remote_rejection_stays_failed_without_auto_replay(workflow):
    workflow[-1].reject = True
    item = finish(workflow)
    assert (
        item.status == "failed" and item.resolved["error_code"] == "provider_rejected"
    )
    assert not workflow[-1].rows and len(writes(workflow)) == 1
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.status == "failed" and effect.remote_id is None
        assert not session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
        ).all()
    for _ in range(3):
        advance(workflow)
    assert len(writes(workflow)) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_same_remote_scope_never_creates_for_a_second_preparation(workflow, unknown):
    remote = workflow[-1]
    remote.lose_reply = unknown
    # 回执丢失且出现两条同参数链接：首个条目停在待核实并继续占用远端范围。
    remote.duplicate_created = unknown
    first = finish(workflow, limit=8)
    assert first.status == ("result_unknown" if unknown else "ready")
    with Session(workflow[0]) as session, session.begin():
        prep = prepare_links(
            session,
            context=workflow[1],
            connection_id=workflow[3],
            application_id="wy-app",
            lines=["Moon"],
            config={"episode": 1},
            request_id=uuid4(),
        )
        item = session.exec(
            select(LinkPreparationItem).where(
                LinkPreparationItem.preparation_id == prep
            )
        ).one()
        identity = item.id
    other = (*workflow[:2], identity, *workflow[3:])
    item = finish(other, limit=8)
    assert item.status == ("pending" if unknown else "ready"), item.resolved
    assert len(writes(workflow)) == 1
    if unknown:
        assert item.resolved["error_code"] == "provider_scope_busy"
    else:
        assert remote.calls[-1][2]["id"] == "901"


def test_worker_exit_after_remote_commit_recovers_without_any_receipt(workflow):
    def worker_exit():
        raise SystemExit("synthetic worker terminated before receiving response")

    workflow[-1].after_post = worker_exit
    with pytest.raises(SystemExit):
        finish(workflow)
    with Session(workflow[0]) as session:
        effect = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).one()
        assert effect.status == "sending" and effect.remote_id is None
        assert not session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "wy_receipt")
        ).all()
    expire_completed_worker_claim(workflow)
    assert finish(workflow).status == "ready"
    assert len(writes(workflow)) == 1
    assert workflow[-1].calls[-1][2]["id"] == "901"
