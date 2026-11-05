"""音乐读取只替换官方 SDK 最终 HTTP 传输；路由/配额使用真实 PG/Redis。"""

import json
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import RemoteCallBudget
from app.integrations.tiktok.sdk import official_client
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


def music_row(identity="music-1", **changes):
    return {
        "music_id": identity,
        "name": None,
        "file_name": "Synthetic title",
        "author": None,
        "duration": 0,
        "url": "https://music.example.test/play?signature=secret-preview",
        **changes,
    }


def envelope(rows=(), *, page=1, total=None, **changes):
    count = len(rows) if total is None else total
    return {
        "code": 0,
        "request_id": "music-evidence",
        "data": {
            "musics": list(rows),
            "page_info": {
                "page": page,
                "page_size": 100,
                "total_page": (count + 99) // 100,
                "total_number": count,
            },
        },
        **changes,
    }


@pytest.fixture
def music_wire(monkeypatch):
    replies, calls = deque(), []

    def http(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        value = replies.popleft()
        if isinstance(value, Exception):
            raise value
        if isinstance(value, HTTPResponse):
            return value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return HTTPResponse(body=BytesIO(body), status=200, preload_content=False)

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", http)
    return replies, calls


@pytest.fixture
def music_sdk(music_wire):
    from app.integrations.tiktok.adapters.sdk_music import SDKMusicOperations

    deadline = datetime.now(UTC) + timedelta(seconds=40)
    budget = RemoteCallBudget(deadline, 45, 60000)
    route = FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc-1",
        connection_id=uuid4(),
        channel="OFFICIAL_API",
        authorization_revision=1,
        binding_revision=2,
        adapter_contract_revision="official-api-v1",
    )
    events = []

    @contextmanager
    def scope(advertiser, operation, actual_deadline):
        assert actual_deadline == deadline
        events.append((advertiser, operation))
        yield

    with official_client(access_token="synthetic-music-token") as client:

        def opened(frozen=route):
            return SDKMusicOperations(
                client, route=frozen, request_scope=scope, deadline=deadline
            )

        yield opened, music_wire[0], music_wire[1], budget, route, events


def test_search_normalizes_title_zero_duration_and_safe_evidence(music_sdk):
    opened, replies, calls, budget, _, events = music_sdk
    replies.append(
        envelope(
            [
                music_row(),
                music_row("music-2", name=" Name "),
                music_row("music-3", file_name=None),
            ]
        )
    )
    result = opened().search(
        advertiser_id="123", keyword="sunshine", cursor=None, budget=budget
    )
    assert [item.display_name for item in result.items] == [
        "Synthetic title",
        "Name",
        "music-3",
    ]
    assert result.items[0].duration == 0
    assert result.items[0].author is None
    assert result.evidence.request_id == "music-evidence"
    assert result.next_cursor is None
    assert "secret-preview" not in repr(result)
    assert "eligible" not in asdict(result.items[0])
    assert events == [("123", "music.get")]
    assert calls[0][0] == "GET"
    args = parse_qs(urlsplit(calls[0][1]).query)
    assert urlsplit(calls[0][1]).path == "/open_api/v1.3/file/music/get/"
    assert args["music_scene"] == ["CAROUSEL_ADS"]
    assert args["search_type"] == ["SEARCH_BY_KEYWORD"]
    assert json.loads(args["filtering"][0]) == {"keyword": "sunshine"}


@pytest.mark.parametrize("count", [2, 35])
def test_recommend_uses_transient_urls_and_same_endpoint(music_sdk, count):
    opened, replies, calls, budget, _, _ = music_sdk
    urls = tuple(f"https://image.example.test/{i}?sig=transient" for i in range(count))
    replies.append(envelope([music_row()]))
    result = opened().recommend(advertiser_id="123", image_urls=urls, budget=budget)
    assert result.items[0].music_id == "music-1"
    query = parse_qs(urlsplit(calls[0][1]).query)
    assert query["search_type"] == ["SEARCH_BY_RECOMMEND"]
    assert json.loads(query["filtering"][0]) == {"image_urls": list(urls)}
    assert "transient" not in repr(result)


@pytest.mark.parametrize(
    "urls",
    [
        (),
        ("https://image.test/1",),
        tuple("https://image.test/1" for _ in range(36)),
        ("image-id", "image-id-2"),
    ],
)
def test_recommend_rejects_invalid_input_without_call(music_sdk, urls):
    opened, _, calls, budget, _, events = music_sdk
    with pytest.raises(DomainError) as error:
        opened().recommend(advertiser_id="123", image_urls=urls, budget=budget)
    assert error.value.code == "music_request_invalid"
    assert not calls and not events


def test_cursor_survives_new_adapter_and_binds_every_route_dimension(music_sdk):
    opened, replies, calls, budget, route, _ = music_sdk
    replies.append(envelope([music_row(f"m-{i}") for i in range(100)], total=101))
    first = opened().search(
        advertiser_id="123", keyword="sunshine", cursor=None, budget=budget
    )
    cursor = first.next_cursor
    assert cursor and "sunshine" not in cursor and "123" not in cursor
    for field, value in {
        "tenant_id": uuid4(),
        "bc_id": "bc-other",
        "connection_id": uuid4(),
        "authorization_revision": 2,
        "binding_revision": 3,
        "adapter_contract_revision": "v-other",
    }.items():
        with pytest.raises(DomainError) as error:
            opened(route.model_copy(update={field: value})).search(
                advertiser_id="123", keyword="sunshine", cursor=cursor, budget=budget
            )
        assert error.value.code == "music_cursor_invalid"
    for account, keyword, value in [
        ("456", "sunshine", cursor),
        ("123", "other", cursor),
        ("123", "sunshine", cursor + "x"),
    ]:
        with pytest.raises(DomainError):
            opened().search(
                advertiser_id=account, keyword=keyword, cursor=value, budget=budget
            )
    assert len(calls) == 1
    replies.append(envelope([music_row("last")], page=2, total=101))
    second = opened().search(
        advertiser_id="123", keyword="sunshine", cursor=cursor, budget=budget
    )
    assert second.next_cursor is None and second.items[0].music_id == "last"
    assert parse_qs(urlsplit(calls[-1][1]).query)["page"] == ["2"]


def test_read_exact_ids_does_not_accept_unrequested_music(music_sdk):
    opened, replies, calls, budget, _, _ = music_sdk
    replies.append(envelope([music_row()]))
    assert (
        opened().read(advertiser_id="123", music_ids=("music-1",), budget=budget).items
    )
    args = parse_qs(urlsplit(calls[0][1]).query)
    assert args["search_type"] == ["SEARCH_BY_MUSIC_ID"]
    assert json.loads(args["filtering"][0]) == {"music_ids": ["music-1"]}
    replies.append(envelope([music_row("unexpected")]))
    with pytest.raises(DomainError) as error:
        opened().read(advertiser_id="123", music_ids=("music-1",), budget=budget)
    assert error.value.code == "unsupported_music_schema"


def test_timeout_is_not_successful_empty_and_errors_are_sanitized(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(envelope())
    assert (
        opened()
        .search(advertiser_id="123", keyword="none", cursor=None, budget=budget)
        .items
        == ()
    )
    replies.append(
        ReadTimeoutError(None, "https://secret.test/?token=private", "private")
    )
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="none", cursor=None, budget=budget)
    assert error.value.code == "music_timeout"
    assert "private" not in repr(error.value)
    replies.append(envelope(code=40002, message="token=private", data=None))
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="none", cursor=None, budget=budget)
    assert error.value.evidence.request_id == "music-evidence"
    assert error.value.evidence.remote_code == 40002
    assert "private" not in repr(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"music_id": 123},
        {"name": []},
        {"duration": True},
        {"duration": -1},
        {"duration": "3"},
        {"duration": float("nan")},
        {"author": {}},
        {"url": "javascript:secret"},
    ],
)
def test_strict_item_schema(music_sdk, change):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(envelope([music_row(**change)]))
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert error.value.code == "unsupported_music_schema"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"code":0,"code":0,"data":{}}',
        json.dumps(envelope(code=False)).encode(),
        json.dumps(envelope(data={"list": [], "page_info": {}})).encode(),
        json.dumps(envelope([music_row(), music_row()])).encode(),
        json.dumps(envelope([], total=101)).encode(),
    ],
)
def test_strict_envelope_page_and_duplicates(music_sdk, raw):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(raw)
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert error.value.code == "unsupported_music_schema"


def test_budget_mismatch_and_expiry_prevent_http(music_sdk):
    opened, _, calls, budget, _, _ = music_sdk
    for invalid in [
        replace(budget, deadline=budget.deadline + timedelta(seconds=1)),
        replace(budget, lease_ms=1),
    ]:
        with pytest.raises(DomainError):
            opened().search(
                advertiser_id="123", keyword="k", cursor=None, budget=invalid
            )
    assert not calls


def test_retired_provider_has_explicit_no_transport_capability():
    from app.integrations.tiktok.contracts.music import UnsupportedMusicOperations

    adapter = UnsupportedMusicOperations()
    budget = RemoteCallBudget(datetime.now(UTC), 45, 60000)
    for method, arguments in [
        (adapter.search, {"keyword": "k", "cursor": None}),
        (adapter.recommend, {"image_urls": ()}),
        (adapter.read, {"music_ids": ()}),
    ]:
        with pytest.raises(DomainError) as error:
            method(advertiser_id="123", budget=budget, **arguments)
        assert error.value.code == "music_channel_unsupported"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_gateway_music_uses_real_route_permission_and_admission(
    database_engine, redis_client, gateway_case, music_wire
):
    from sqlmodel import Session, select

    from app.core.config import settings
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from app.jobs.admission import admission_keys
    from app.modules.accounts.models import BCAccountAccess

    context, route, account = gateway_case
    replies, calls = music_wire
    deadline = datetime.now(UTC) + timedelta(seconds=20)
    budget = RemoteCallBudget(deadline, 25, 40000)
    replies.append(envelope())
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        gateway.music.search(
            advertiser_id=account, keyword="k", cursor=None, budget=budget
        )
        with Session(database_engine) as session:
            grant = session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == context.tenant_id
                )
            ).one()
            grant.active = False
            session.add(grant)
            session.commit()
        with pytest.raises(DomainError):
            gateway.music.search(
                advertiser_id=account, keyword="k", cursor=None, budget=budget
            )
    assert len(calls) == 1
    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        "music.get",
        context.tenant_id,
        account,
        "GET /open_api/v1.3/file/music/get/",
    )
    assert redis_client.zcard(keys[0]) == 1 and redis_client.zcard(keys[1]) == 1
    assert redis_client.zcard(keys[2]) == 0


def test_recommend_first_page_marks_truncated_without_fabricated_cursor(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(envelope([music_row(f"m-{i}") for i in range(100)], total=101))
    result = opened().recommend(
        advertiser_id="123",
        image_urls=("https://images.test/1", "https://images.test/2"),
        budget=budget,
    )
    assert result.truncated is True and result.next_cursor is None
    assert len(result.items) == 100


def test_recommend_accepts_actual_page_size_returned_by_platform(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    response = envelope([music_row(f"m-{i}", duration=47) for i in range(98)])
    response["data"]["page_info"]["page_size"] = 98
    replies.append(response)
    result = opened().recommend(
        advertiser_id="123",
        image_urls=("https://images.test/1", "https://images.test/2"),
        budget=budget,
    )
    assert len(result.items) == 98
    assert result.next_cursor is None and result.truncated is False


def test_read_missing_ids_stay_missing_and_page_mismatch_rejected(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(envelope())
    assert (
        opened().read(advertiser_id="123", music_ids=("missing",), budget=budget).items
        == ()
    )
    replies.append(envelope([music_row()], page=2, total=101))
    with pytest.raises(DomainError) as error:
        opened().read(advertiser_id="123", music_ids=("music-1",), budget=budget)
    assert error.value.code == "unsupported_music_schema"


@pytest.mark.parametrize("account", [object(), None, 123, [], "", " 123"])
def test_invalid_account_rejected_before_cursor_serialization(music_sdk, account):
    opened, _, calls, budget, _, events = music_sdk
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id=account, keyword="k", cursor=None, budget=budget)
    assert error.value.code == "music_request_invalid"
    assert not calls and not events


@pytest.mark.parametrize(
    "ids", [(), ("m", "m"), (True,), (" m",), tuple(str(i) for i in range(101))]
)
def test_invalid_read_filter_never_sends(music_sdk, ids):
    opened, _, calls, budget, _, events = music_sdk
    with pytest.raises(DomainError) as error:
        opened().read(advertiser_id="123", music_ids=ids, budget=budget)
    assert error.value.code == "music_request_invalid"
    assert not calls and not events


def test_oversized_body_and_unsafe_request_id_do_not_leak(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    replies.append(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert error.value.code == "unsupported_music_schema"
    replies.append(envelope(request_id="https://private.test/token=private"))
    page = opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert page.evidence.request_id is None
    assert "private" not in repr(page)


def test_provider_throttle_is_deferred_not_empty(music_sdk):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    opened, replies, _, budget, _, _ = music_sdk
    replies.append(envelope(code=40100, message="private provider message"))
    with pytest.raises(AccountAdmissionDeferred) as error:
        opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert error.value.admission_source == "provider"
    assert "private" not in repr(error.value)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_each_music_method_shares_quota_and_denial_never_sends(
    database_engine, redis_client, gateway_case, music_wire, monkeypatch
):
    from app.core.config import settings
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admission_keys

    context, route, account = gateway_case
    replies, calls = music_wire
    config = settings.TIKTOK_CALL_POLICIES
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": {**config["base"], "window_ms": 60000, "app_calls_per_window": 3}},
    )
    deadline = datetime.now(UTC) + timedelta(seconds=20)
    budget = RemoteCallBudget(deadline, 25, 40000)
    replies.extend([envelope(), envelope(), envelope()])
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        gateway.music.search(
            advertiser_id=account, keyword="k", cursor=None, budget=budget
        )
        gateway.music.recommend(
            advertiser_id=account,
            image_urls=("https://image.test/1", "https://image.test/2"),
            budget=budget,
        )
        gateway.music.read(advertiser_id=account, music_ids=("missing",), budget=budget)
        with pytest.raises(AccountAdmissionDeferred):
            gateway.music.search(
                advertiser_id=account, keyword="k", cursor=None, budget=budget
            )
    assert len(calls) == 3
    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        "music.get",
        context.tenant_id,
        account,
        "GET /open_api/v1.3/file/music/get/",
    )
    assert redis_client.zcard(keys[0]) == 3 and redis_client.zcard(keys[1]) == 3
    assert redis_client.zcard(keys[2]) == 0


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_gateway_changed_binding_rejects_music_without_send(
    database_engine, redis_client, gateway_case, music_wire
):
    from sqlmodel import Session, select

    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from app.modules.accounts.connection_models import BCConnectionBinding

    context, route, account = gateway_case
    replies, calls = music_wire
    deadline = datetime.now(UTC) + timedelta(seconds=20)
    budget = RemoteCallBudget(deadline, 25, 40000)
    replies.append(envelope())
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        gateway.music.search(
            advertiser_id=account, keyword="k", cursor=None, budget=budget
        )
        with Session(database_engine) as session:
            binding = session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == context.tenant_id
                )
            ).one()
            binding.revision += 1
            session.add(binding)
            session.commit()
        with pytest.raises(DomainError):
            gateway.music.search(
                advertiser_id=account, keyword="k", cursor=None, budget=budget
            )
    assert len(calls) == 1


def test_read_refuses_truncated_id_evidence(music_sdk):
    opened, replies, _, budget, _, _ = music_sdk
    identities = tuple(f"m-{i}" for i in range(100))
    replies.append(
        envelope([music_row(identity) for identity in identities], total=101)
    )
    with pytest.raises(DomainError) as error:
        opened().read(advertiser_id="123", music_ids=identities, budget=budget)
    assert error.value.code == "music_read_incomplete"
    assert error.value.evidence.request_id == "music-evidence"


def test_expired_session_budget_never_sends(music_sdk):
    from app.integrations.tiktok.adapters.sdk_music import SDKMusicOperations

    opened, _, calls, _, route, events = music_sdk
    deadline = datetime.now(UTC) - timedelta(seconds=1)
    adapter = SDKMusicOperations(
        opened()._client, route=route, request_scope=opened()._scope, deadline=deadline
    )
    with pytest.raises(DomainError) as error:
        adapter.search(
            advertiser_id="123",
            keyword="k",
            cursor=None,
            budget=RemoteCallBudget(deadline, 45, 60000),
        )
    assert error.value.code == "material_deadline"
    assert not calls and not events


def test_music_preview_and_credentials_never_reach_logs(music_sdk, caplog):
    import logging

    opened, replies, _, budget, _, _ = music_sdk
    caplog.set_level(logging.DEBUG)
    replies.append(envelope([music_row()]))
    result = opened().recommend(
        advertiser_id="123",
        image_urls=(
            "https://images.test/1?signature=private-image",
            "https://images.test/2?signature=private-image",
        ),
        budget=budget,
    )
    assert result.items[0].preview_url.endswith("secret-preview")
    for secret in ("private-image", "secret-preview", "synthetic-music-token"):
        assert secret not in caplog.text
        assert secret not in repr(result)


class ObservedMusicResponse(HTTPResponse):
    """只在传输边界观察正文读取及连接回收，不替换 SDK 异常构造。"""

    def __init__(self, *, status, body, read_error=None, headers=None):
        self.read_sizes = []
        self.was_closed = False
        self.was_released = False
        self.read_error = read_error
        super().__init__(
            body=BytesIO(body), status=status, headers=headers, preload_content=False
        )

    def read(self, amt=None, **kwargs):
        self.read_sizes.append(amt)
        if self.read_error is not None:
            raise self.read_error
        return super().read(amt, **kwargs)

    def close(self):
        self.was_closed = True
        return super().close()

    def release_conn(self):
        self.was_released = True
        return super().release_conn()


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("size", [128, 2 * 1024 * 1024 + 1024])
def test_non_success_music_body_never_reaches_sdk_exception(music_sdk, status, size):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    opened, replies, calls, budget, _, _ = music_sdk
    response = ObservedMusicResponse(
        status=status, body=b"private-body" + b"x" * size, headers={"Retry-After": "7"}
    )
    replies.append(response)
    adapter = opened()
    manager = adapter._client.rest_client.pool_manager
    original_request = manager.request
    with pytest.raises(DomainError) as error:
        adapter.search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    if status == 429:
        assert isinstance(error.value, AccountAdmissionDeferred)
        assert error.value.retry_after_ms == 7000
    else:
        assert error.value.code == "music_request_failed"
    # 非成功正文不是音乐证据；应在 SDK ApiException 的 .data 全量读取前直接丢弃。
    assert manager.request == original_request
    assert response.read_sizes == []
    assert response.was_closed and response.was_released
    assert len(calls) == 1 and "private-body" not in repr(error.value)


@pytest.mark.parametrize("status", [200, 503])
def test_music_response_read_failure_closes_transport(music_sdk, status):
    opened, replies, _, budget, _, _ = music_sdk
    response = ObservedMusicResponse(
        status=status,
        body=b"",
        read_error=ReadTimeoutError(None, "private-url", "private-body"),
    )
    replies.append(response)
    with pytest.raises(DomainError) as error:
        opened().search(advertiser_id="123", keyword="k", cursor=None, budget=budget)
    assert error.value.code == (
        "music_timeout" if status == 200 else "music_request_failed"
    )
    assert response.read_sizes == ([2 * 1024 * 1024 + 1] if status == 200 else [])
    assert response.was_closed and response.was_released
    assert "private" not in repr(error.value)
