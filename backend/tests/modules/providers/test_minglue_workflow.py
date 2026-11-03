"""明略连接和持久取链端到端回归：真实 PostgreSQL，模拟远端 HTTP。"""

import json
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.modules.providers.connections import (
    open_provider_session,
    save_connection,
    verify_connection,
)
from app.modules.providers.link_steps import TERMINAL, run_link_item
from app.modules.providers.models import (
    LinkPreparationItem,
    ProviderConnection,
    ProviderEffect,
)
from app.modules.providers.service import choose_drama_candidate, prepare_links
from tests.modules.conftest import create_context
from tests.modules.providers.test_minglue_protocol import response
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)


@dataclass
class Remote:
    links: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    bodies: list = field(default_factory=list)
    visible: bool = True
    timeout: bool = False
    ambiguous: bool = False
    wrong_detail: bool = False
    expired: bool = False
    hide_created: bool = False
    sections_page_size: int = 2

    def __call__(self, req):
        path = req.url.path.removeprefix("/api")
        self.calls.append(path)
        if path == "/Home/Config":
            return response({"opId": 1000})
        if path == "/Home/Captcha":
            return response({"captchaId": "challenge", "imageBytes": "cG5n"})
        if path == "/Home/Login":
            self.expired = False
            return response("renewed-token")
        if self.expired:
            return response(None, code=401)
        if path == "/App/List":
            return response(
                {
                    "items": [
                        {
                            "id": 9,
                            "name": "HaClip",
                            "partnerId": 7,
                            "packageType": 4,
                            "contentType": 3,
                        }
                    ],
                    "totalItems": 1,
                }
            )
        params = dict(req.url.params)
        if req.method == "GET":
            assert params["partnerId"] == "7" and params["appId"] == "9"
        if path == "/Content/List":
            rows = [{"id": 12, "name": "Moon"}]
            if self.ambiguous:
                rows.append({"id": 13, "name": "Moon"})
            return response({"items": rows, "totalItems": len(rows)})
        if path == "/Content/CheckPromotionVisibility":
            return response({"visible": self.visible})
        if path == "/Section/List":
            # 第二页第 101 集：证明选集不以数组位置代替真实集号。
            if self.sections_page_size == 101:
                rows = [
                    {"id": str(1000 + i), "name": f"第{i}集", "sortNo": i}
                    for i in range(1, 102)
                ]
                start = (int(params["pageIndex"]) - 1) * 100
                return response({"items": rows[start : start + 100], "totalItems": 101})
            return response(
                {
                    "items": [
                        {"id": "101", "name": "第1集"},
                        {"id": "102", "name": "第3集"},
                    ],
                    "totalItems": 2,
                }
            )
        if path == "/Promotion/List":
            return response(
                {
                    "items": [] if self.hide_created else self.links,
                    "totalItems": 0 if self.hide_created else len(self.links),
                }
            )
        if path == "/Promotion/Save":
            body = json.loads(req.content)
            assert body["partnerId"] == "7" and body["appId"] == "9"
            self.bodies.append(body)
            self.links.append(
                {
                    **body,
                    "id": "20",
                    "tkMinisUrl": "https://www.tiktok.com/minis/example",
                }
            )
            if self.timeout:
                raise httpx.ReadTimeout("response lost", request=req)
            return response({"id": "20", "tkMinisUrl": self.links[-1]["tkMinisUrl"]})
        if path == "/Promotion/Detail":
            assert params["promotionId"] == "20"
            detail = self.links[-1]
            return response(
                {**detail, **({"sectionId": "999"} if self.wrong_detail else {})}
            )
        raise AssertionError(req.url)


@pytest.fixture
def setup(isolated_strategy_database, monkeypatch):  # noqa: F811
    engine, _, _ = isolated_strategy_database
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.recognize_captcha",
        lambda _, **kwargs: "AB12",
    )
    remote = Remote()
    transport = httpx.MockTransport(remote)
    with Session(engine) as session, session.begin():
        context = create_context(session, role="tenant_admin")
        row = save_connection(
            session,
            context=context,
            kind="minglue",
            display_name="明略测试",
            credentials={"account": "fixture", "password": "private"},
        )
        connection_id = row.id
    verify_connection(
        database_engine=engine,
        context=context,
        connection_id=connection_id,
        transport=transport,
    )
    with Session(engine) as session:
        row = session.get(ProviderConnection, connection_id)
        assert row.status == "active"
        assert (
            decrypt_credentials(
                tenant_id=context.tenant_id, ciphertext=row.encrypted_credentials
            )["token"]
            == "renewed-token"
        )
    return engine, context, connection_id, remote, transport


def prepare(setup, config=None):
    engine, context, connection_id, _, _ = setup
    with Session(engine) as session, session.begin():
        prep = prepare_links(
            session,
            context=context,
            connection_id=connection_id,
            application_id="9",
            lines=["Moon"],
            config=config or {},
            request_id=uuid4(),
        )
        return session.exec(
            select(LinkPreparationItem.id).where(
                LinkPreparationItem.preparation_id == prep
            )
        ).one()


def advance(setup, item_id, *, stop_unknown=True):
    engine, context, _, _, transport = setup
    for _ in range(20):
        with Session(engine) as session:
            run_link_item(
                session, context=context, item_id=item_id, transport=transport
            )
        with Session(engine) as session:
            item = session.get(LinkPreparationItem, item_id)
            if item.status in TERMINAL or (
                stop_unknown and item.status == "result_unknown"
            ):
                return item
    pytest.fail(f"did not finish: {item.status}: {item.resolved}")


def test_create_and_reuse_link_with_exact_readback(setup):
    item = advance(setup, prepare(setup))
    assert item.status == "ready", item.resolved
    remote = setup[3]
    assert remote.bodies[0]["sectionId"] == "101"
    assert remote.calls.count("/Promotion/Detail") == 1
    second = advance(setup, prepare(setup))
    assert second.status == "ready", second.resolved
    assert len(remote.bodies) == 1
    assert remote.calls.count("/Promotion/Detail") == 2


def test_selected_episode_uses_actual_section_and_missing_episode_is_conflict(setup):
    item = advance(setup, prepare(setup, {"episode": 3}))
    assert item.status == "ready", item.resolved
    assert setup[3].bodies[0]["sectionId"] == "102"
    missing = advance(setup, prepare(setup, {"episode": 2}))
    assert missing.status == "config_conflict"
    assert len(setup[3].bodies) == 1


def test_sections_resume_across_pages(setup):
    setup[3].sections_page_size = 101
    item = advance(setup, prepare(setup, {"episode": 101}))
    assert item.status == "ready", item.resolved
    assert setup[3].calls.count("/Section/List") == 2
    assert setup[3].bodies[0]["sectionId"] == "1101"


def test_invisible_drama_never_writes(setup):
    setup[3].visible = False
    item = advance(setup, prepare(setup))
    assert item.status == "blocked_auth"
    assert not setup[3].bodies


def test_ambiguous_selection_still_checks_visibility_and_sections(setup):
    setup[3].ambiguous = True
    item_id = prepare(setup)
    item = advance(setup, item_id)
    assert item.status == "needs_resolution"
    with Session(setup[0]) as session, session.begin():
        choose_drama_candidate(
            session, context=setup[1], input_id=item_id, external_drama_id="12"
        )
    item = advance(setup, item_id)
    assert item.status == "ready", item.resolved
    assert "/Content/CheckPromotionVisibility" in setup[3].calls


def test_create_timeout_recovers_without_replaying_write(setup):
    setup[3].timeout = True
    item_id = prepare(setup)
    first = advance(setup, item_id)
    assert first.status == "result_unknown"
    item = advance(setup, item_id)
    assert item.status == "ready", item.resolved
    assert len(setup[3].bodies) == 1
    with Session(setup[0]) as session:
        effects = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == setup[1].tenant_id,
                ProviderEffect.step == "create",
            )
        ).all()
        assert len(effects) == 1 and effects[0].status == "succeeded"


def test_unknown_write_stays_unknown_when_list_is_empty(setup):
    setup[3].timeout = True
    item_id = prepare(setup)
    assert advance(setup, item_id).status == "result_unknown"
    setup[3].hide_created = True
    for _ in range(3):
        assert advance(setup, item_id).status == "result_unknown"
    assert len(setup[3].bodies) == 1


def test_wrong_section_readback_never_publishes(setup):
    setup[3].wrong_detail = True
    item = advance(setup, prepare(setup))
    assert item.status == "config_conflict", item.resolved
    assert item.resolved["error_code"] == "config_conflict"


def test_expired_session_renews_and_resumes_same_item(setup):
    item_id = prepare(setup)
    setup[3].expired = True
    # 登录和应用重发现各持久化一轮，然后继续原任务。
    item = advance(setup, item_id)
    assert item.status == "ready", item.resolved
    assert setup[3].calls.count("/Home/Login") == 2
    assert len(setup[3].bodies) == 1


def test_foreign_tenant_cannot_open_connection(setup):
    with Session(setup[0]) as session, session.begin():
        foreign = create_context(session, role="tenant_admin")
    calls = len(setup[3].calls)
    with pytest.raises(DomainError) as error:
        with open_provider_session(
            database_engine=setup[0],
            context=foreign,
            connection_id=setup[2],
            application_id="9",
            transport=setup[4],
        ):
            pytest.fail("foreign connection opened")
    assert error.value.code == "resource_not_found"
    assert len(setup[3].calls) == calls


def test_wrong_app_binding_cannot_open_provider(setup):
    calls = len(setup[3].calls)
    with pytest.raises(DomainError):
        with open_provider_session(
            database_engine=setup[0],
            context=setup[1],
            connection_id=setup[2],
            application_id="99",
            transport=setup[4],
        ):
            pytest.fail("foreign application opened")
    assert len(setup[3].calls) == calls


def test_same_remote_scope_serializes_interleaved_preparations(setup):
    first, second = prepare(setup), prepare(setup)
    # 两个任务都完成搜剧、可推广性和剧集读取，然后竞争同一应用/剧/剧集 scope。
    for item_id in (first, second):
        for _ in range(3):
            with Session(setup[0]) as session:
                run_link_item(
                    session, context=setup[1], item_id=item_id, transport=setup[4]
                )
    with Session(setup[0]) as session:
        run_link_item(session, context=setup[1], item_id=first, transport=setup[4])
    with Session(setup[0]) as session:
        run_link_item(session, context=setup[1], item_id=second, transport=setup[4])
    with Session(setup[0]) as session:
        item = session.get(LinkPreparationItem, second)
        assert item.resolved["error_code"] == "provider_scope_busy"
    assert advance(setup, first).status == "ready"
    assert advance(setup, second).status == "ready"
    assert len(setup[3].bodies) == 1
