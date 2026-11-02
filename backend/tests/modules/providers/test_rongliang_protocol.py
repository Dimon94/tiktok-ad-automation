import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.rongliang import RongliangClient


@pytest.mark.parametrize(
    ("title", "expected_ids"),
    [
        ("Moon Show", ["8453", "8454"]),
        ("Moon", ["8453"]),
        ("Moon Sho", []),
        ("moon show", []),
    ],
)
def test_rongliang_search_matches_current_or_original_title_exactly(title, expected_ids):
    # 当前剧名与原名不同；同名多剧保留给上层消歧，近似名称不能自动选中。
    def handle(request):
        assert request.method == "GET"
        assert request.url.params["compilationsName"] == title
        return httpx.Response(
            200,
            json={"records": [
                {"compilationsId": 8453, "compilationsName": "Moon Show", "originalTitle": "Moon"},
                {"compilationsId": 8454, "compilationsName": "Moon Show", "originalTitle": "Another Moon"},
                {"compilationsId": 8455, "compilationsName": "Moon Show Extended", "originalTitle": "Extended"},
            ], "total": 3},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        page = RongliangClient(http).search(title, None)
    assert [item.external_drama_id for item in page.items] == expected_ids
    assert all(item.title == title for item in page.items)
    assert page.complete


def test_rongliang_form_login_form_data_search_and_strict_create_readback():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/auth/login"):
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert request.content == b"email=user%40example.test&password=private-password"
            return httpx.Response(
                200,
                json={"token": "dist-jwt", "userName": "User"},
                headers=[
                    ("set-cookie", "dist_token=dist-jwt; Path=/"),
                    (
                        "set-cookie",
                        "dist_current_user=%7B%22userId%22%3A%22u-1%22%7D; Path=/",
                    ),
                ],
            )
        assert "dist_token=dist-jwt" in request.headers["cookie"]
        assert "dist_current_user=%7B%22userId%22%3A%22u-1%22%7D" in request.headers["cookie"]
        if request.url.path.endswith("/link/form_data"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"packageOptions": [{"clientId": 1144, "clientName": "Cove", "clientType": "TIKTOK_MINI_PROGRAM"}]}},
            )
        if request.url.path.endswith("/compilations/page"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"records": [{"compilationsId": 8453, "originalTitle": "Moon", "compilationsName": "Moon Show"}], "total": 1}},
            )
        if request.url.path.endswith("/episodic_dramas"):
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"episodicDramaId": 991, "title": "1"}]},
            )
        if request.url.path.endswith("/link/page"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"records": [{"batchId": "B-1", "clientId": 1144, "compilationsId": 8453, "episodicDramaId": 991, "deliverPlatform": 1}], "total": 1}},
            )
        if request.url.path.endswith("/link/create"):
            assert json.loads(request.content) == {
                "clientId": 1144,
                "deliverPlatform": 1,
                "deliveryType": 1,
                "linkType": 2,
                "compilationsId": 8453,
                "compilationsAlias": "Moon",
                "episodicDramaId": 991,
            }
            return httpx.Response(200, json={"code": 0, "data": True})
        if request.url.path.endswith("/link/url"):
            assert request.url.params["batchId"] == "B-1"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"deepLink": "https://www.tiktok.com/minis/rl?batchId=B-1", "planName": "Moon", "adGroupName": "Moon Ads"}},
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient.login(http, email="user@example.test", password="private-password")
        assert client.discover_applications() == [
            {
                "external_id": "1144",
                "name": "Cove",
                "channel_config": {"platform": 1},
                "tiktok_minis_id": None,
            }
        ]
        page = client.search("Moon", None)
        assert page.items[0].external_drama_id == "8453"
        receipt = client.create_link(
            "8453",
            {
                "client_id": "1144",
                "episodic_drama_id": "991",
                "platform": 1,
                "delivery_type": 1,
                "link_type": 2,
                "alias": "Moon",
            },
        )
    assert receipt.remote_id == "B-1"
    assert receipt.protected_base == "Moon"
    assert receipt.attribution["adGroupName"] == "Moon Ads"
    assert len(seen) == 6


@pytest.mark.parametrize("response", [True, {"code": 0, "data": True}])
def test_rongliang_create_without_matching_batch_is_result_unknown(response):
    def handle(request):
        if request.url.path.endswith("/link/create"):
            return httpx.Response(200, json=response)
        if request.url.path.endswith("/link/page"):
            return httpx.Response(200, json={"code": 0, "data": {"records": [], "total": 0}})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient(http, token="dist-jwt")
        with pytest.raises(DomainError) as error:
            client.create_link(
                "8453",
                {"client_id": "1144", "episodic_drama_id": "991", "platform": 1},
            )
    assert error.value.code == "provider_result_unknown"


def test_rongliang_read_link_returns_verifiable_configuration():
    def handle(request):
        if request.url.path.endswith("/link/page"):
            assert request.url.params["batchId"] == "B-1"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "records": [
                            {
                                "batchId": "B-1",
                                "clientId": 1144,
                                "episodicDramaId": 991,
                                "deliverPlatform": 1,
                                "deliveryType": 1,
                                "linkType": 2,
                                "compilationsAlias": "Moon",
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        if request.url.path.endswith("/link/url"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "deepLink": "https://www.tiktok.com/minis/rl?batchId=B-1",
                        "planName": "Moon",
                        "adGroupName": "Moon Ads",
                    },
                },
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        receipt = RongliangClient(http, token="dist-jwt").read_link("B-1")
    assert receipt.config == {
        "client_id": 1144,
        "episodic_drama_id": 991,
        "platform": 1,
        "delivery_type": 1,
        "link_type": 2,
        "alias": "Moon",
    }


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"code": "user-2", "msg": "expired"}),
        # 2026-10-24 实测：容量未登录返回 HTTP 500 + code=403。
        (500, {"code": 403, "msg": "Unauthorized"}),
        (401, {"code": "401"}),
    ],
)
def test_rongliang_token_expiry_is_stable(status, body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    ) as http:
        client = RongliangClient(http, token="expired")
        with pytest.raises(DomainError) as error:
            client.discover_applications()
    assert error.value.code == "provider_session_expired"


def test_rongliang_server_error_without_auth_code_stays_retryable():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"code": 500}))
    ) as http:
        client = RongliangClient(http, token="valid")
        with pytest.raises(DomainError) as error:
            client.discover_applications()
    assert error.value.code == "provider_unavailable" and error.value.retryable


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"aliasList": ["罪爱难逃", "备用"], "originalTitle": "Broken Altar"}, "罪爱难逃"),
        ({"aliasList": [], "originalTitle": "Broken Altar"}, "Broken Altar"),
        ({"aliasList": [""], "originalTitle": "Broken Altar"}, "Broken Altar"),
        ({"aliasList": ["  "], "originalTitle": "Broken Altar"}, "Broken Altar"),
    ],
)
def test_rongliang_alias_follows_web_form(payload, expected):
    def handle(request):
        assert request.url.path.endswith("/link/compilations_alias")
        assert request.url.params["compilationsId"] == "77"
        return httpx.Response(200, json={"code": 0, "data": payload})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient(http, token="t")
        assert client.compilations_alias("77") == expected


@pytest.mark.parametrize(
    "payload",
    [{"aliasList": None}, {"aliasList": [""], "originalTitle": ""}, ["x"], None],
)
def test_rongliang_alias_missing_raises_instead_of_empty(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 0, "data": payload}))
    ) as http:
        client = RongliangClient(http, token="t")
        with pytest.raises(DomainError) as error:
            client.compilations_alias("77")
    assert error.value.code == "provider_schema_unsupported"


@pytest.mark.parametrize("transport_kind", ["httpx", "curl"])
@pytest.mark.parametrize("create_response", [True, {"code": 0, "data": True}, {"code": "duplicate"}, None])
def test_rongliang_create_readback_works_for_both_transports(transport_kind, create_response):
    from types import SimpleNamespace

    from app.modules.providers.adapters.rongliang import _CurlSession

    writes = []
    def handle(request):
        if request.url.path.endswith("/link/create"):
            writes.append(json.loads(request.content))
            return httpx.Response(200, content=json.dumps(create_response))
        if request.url.path.endswith("/link/page"):
            return httpx.Response(200, json={"records": [{
                "batchId": "B-1", "clientId": 1144, "compilationsId": 8453,
                "episodicDramaId": 991, "deliverPlatform": 1,
                "deliveryType": 1, "linkType": 2,
            }], "total": 1})
        if request.url.path.endswith("/link/url"):
            return httpx.Response(200, json={"deepLink": "https://www.tiktok.com/minis/rl?batchId=B-1", "planName": "Moon"})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        curl = None
        if transport_kind == "curl":
            # 保留真实 Curl 包装及响应分类，只替换最底层网络传输。
            curl = _CurlSession.__new__(_CurlSession)
            def request(method, url, **kwargs):
                return handle(httpx.Request(method, url, json=kwargs.get("json")))
            curl.session = SimpleNamespace(request=request, close=lambda: None)
        client = RongliangClient(http, token="test-token", curl_session=curl)
        receipt = client.create_link("8453", {"client_id": "1144", "episodic_drama_id": 991})
    assert receipt.url == "https://www.tiktok.com/minis/rl?batchId=B-1"
    assert receipt.config["client_id"] == 1144
    assert len(writes) == 1


def test_rongliang_real_rejection_without_matching_link_stays_failed():
    def handle(request):
        if request.url.path.endswith("/link/create"):
            return httpx.Response(200, json={"code": "forbidden-drama"})
        if request.url.path.endswith("/link/page"):
            return httpx.Response(200, json={"records": [{
                "batchId": "other", "clientId": 1144, "compilationsId": 8453,
                "episodicDramaId": 992, "deliverPlatform": 1,
            }], "total": 1})
        raise AssertionError("Must not read an unrelated link")
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(DomainError) as error:
            RongliangClient(http).create_link("8453", {"client_id": "1144", "episodic_drama_id": 991})
    assert error.value.code == "provider_rejected"


def test_rongliang_incomplete_readback_defers_to_durable_recovery():
    def handle(request):
        if request.url.path.endswith("/link/create"):
            return httpx.Response(200, json=True)
        assert request.url.params["pageNumber"] == "1", "Paging belongs to durable recovery"
        return httpx.Response(200, json={"records": [], "total": 200})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(DomainError) as error:
            RongliangClient(http).create_link("8453", {"client_id": "1144", "episodic_drama_id": 991})
    assert error.value.code == "provider_result_unknown"
