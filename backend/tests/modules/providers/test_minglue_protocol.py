"""明略 CLI 协议的离线回归；只在 HTTP 传输边界模拟远端。"""

import base64
import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.minglue import MinglueClient

APP = {"partner_id": "7", "package_type": 4, "content_type": 3}
CONFIG = {
    "partner_id": "7",
    "app_id": "9",
    "content_id": "12",
    "section_id": "101",
    "episode": 1,
    "content_type": 3,
}


def response(data, code=0, msg=""):
    return httpx.Response(200, json={"code": code, "msg": msg, "data": data})


def client(handler):
    return MinglueClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        token="secret",
        application_id="9",
        channel_config=APP,
    )


def row(**values):
    return {
        "id": "20",
        "partnerId": "7",
        "appId": "9",
        "contentId": "12",
        "sectionId": "101",
        "tkMinisUrl": "https://www.tiktok.com/minis/example",
        "name": "Moon",
        **values,
    }


def test_login_captcha_retry_and_plain_token(monkeypatch):
    calls, attempts = [], []
    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.recognize_captcha",
        lambda image, **kwargs: "AB12",
    )

    def remote(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/Config"):
            return response({"opId": 1000})
        if req.url.path.endswith("/Captcha"):
            return response(
                {
                    "captchaId": "challenge",
                    "imageBytes": base64.b64encode(b"png").decode(),
                }
            )
        if req.url.path.endswith("/Login"):
            body = json.loads(req.content)
            assert body == {
                "loginName": "user",
                "loginPass": "password",
                "captchaCode": "AB12",
                "captchaCodeKey": "challenge",
                "opId": 1000,
                "scene": 1,
            }
            attempts.append(body)
            return (
                response("secret")
                if len(attempts) == 2
                else response(None, 1, "验证码错误")
            )
        assert req.headers["Authorization"] == "secret"
        return response(
            {
                "items": [
                    {
                        "id": 9,
                        "name": "HaClip",
                        "partnerId": 7,
                        "packageType": 4,
                        "contentType": 3,
                    },
                    {"id": 10, "name": "H5", "partnerId": 7, "packageType": 1},
                ],
                "totalItems": 2,
            }
        )

    with httpx.Client(transport=httpx.MockTransport(remote)) as http:
        c = MinglueClient.login(http, account="user", password="password")
        apps = c.discover_applications()
    assert len(attempts) == 2
    assert apps == [
        {
            "external_id": "9",
            "name": "HaClip",
            "channel_config": APP,
            "tiktok_minis_id": None,
        }
    ]


def test_login_password_failure_does_not_retry(monkeypatch):
    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.recognize_captcha",
        lambda _, **kwargs: "AB12",
    )
    attempts = []

    def remote(req):
        if req.url.path.endswith("/Config"):
            return response({"opId": 1000})
        if req.url.path.endswith("/Captcha"):
            return response({"captchaId": "c", "imageBytes": "cG5n"})
        attempts.append(req)
        return response(None, 1, "账号或密码错误")

    with pytest.raises(DomainError) as error:
        MinglueClient.login(
            httpx.Client(transport=httpx.MockTransport(remote)),
            account="user",
            password="bad",
        )
    assert error.value.code == "provider_auth_failed"
    assert len(attempts) == 1


def test_search_exact_title_context_and_pagination():
    def remote(req):
        assert req.headers["Authorization"] == "secret"
        assert dict(req.url.params)["partnerId"] == "7"
        assert dict(req.url.params)["appId"] == "9"
        return response(
            {
                "items": [
                    {"id": 12, "name": "Moon", "code": "ML12", "feeSectionStart": 2},
                    *[{"id": 13 + i, "name": "Moonlight"} for i in range(99)],
                ],
                "totalItems": 102,
            }
        )

    page = client(remote).search("Moon", None)
    assert not page.complete and page.next_cursor == "2"
    assert [d.external_drama_id for d in page.items] == ["12"]
    assert page.items[0].free_episode_count is None


@pytest.mark.parametrize(
    "data",
    [
        {"items": []},
        {"items": [], "totalItems": 2},
        {"items": [], "totalItems": -1},
        {"items": [row()], "totalItems": 2},
    ],
)
def test_incomplete_pages_cannot_prove_absence(data):
    with pytest.raises(DomainError):
        client(lambda _: response(data)).lookup_link("12", CONFIG, None)


def test_lookup_requires_same_app_drama_section_and_minis_url():
    data = [
        row(),
        row(id="21", appId="99"),
        row(id="22", sectionId="102"),
        row(id="23", contentId="13"),
    ]
    page = client(
        lambda _: response({"items": data, "totalItems": len(data)})
    ).lookup_link("12", CONFIG, None)
    assert [r["id"] for r in page.items] == ["20"]


def test_create_uses_minis_fields_and_readback_proves_scope():
    bodies = []

    def remote(req):
        if req.method == "POST":
            body = json.loads(req.content)
            bodies.append(body)
            assert body["partnerId"] == "7" and body["appId"] == "9"
            assert body["contentId"] == "12" and body["sectionId"] == "101"
            assert (
                body["coinPerSection"]
                == body["feeSectionStart"]
                == body["pixelCode"]
                == ""
            )
            return response({"id": 20, "tkMinisUrl": row()["tkMinisUrl"]})
        assert req.url.params["promotionId"] == "20"
        return response(row())

    c = client(remote)
    assert c.create_link("12", CONFIG).remote_id == "20"
    read = c.read_link("20")
    assert read.config["section_id"] == "101"
    assert read.attribution["contentId"] == "12"
    assert len(bodies) == 1


@pytest.mark.parametrize(
    "values", [{"appId": "99"}, {"partnerId": "99"}, {"tkMinisUrl": ""}, {"id": "99"}]
)
def test_readback_rejects_wrong_scope_or_link(values):
    with pytest.raises(DomainError):
        client(lambda _: response(row(**values))).read_link("20")


def test_body_401_is_session_expired_and_write_timeout_is_unknown():
    with pytest.raises(DomainError) as expired:
        client(lambda _: response(None, 401)).search("Moon", None)
    assert expired.value.code == "provider_session_expired"

    def timeout(req):
        raise httpx.ReadTimeout("lost", request=req)

    with pytest.raises(DomainError) as unknown:
        client(timeout).create_link("12", CONFIG)
    assert unknown.value.code == "provider_result_unknown"


def test_captcha_exhaustion_is_distinct_from_bad_password(monkeypatch):
    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.recognize_captcha",
        lambda _, **kwargs: "AB12",
    )
    attempts = []

    def remote(req):
        if req.url.path.endswith("/Config"):
            return response({"opId": 1000})
        if req.url.path.endswith("/Captcha"):
            return response({"captchaId": "c", "imageBytes": "cG5n"})
        attempts.append(req)
        return response(None, 1, "验证码已过期")

    with pytest.raises(DomainError) as error:
        MinglueClient.login(
            httpx.Client(transport=httpx.MockTransport(remote)),
            account="user",
            password="secret",
        )
    assert error.value.code == "provider_captcha_failed"
    assert len(attempts) == 2


def test_ocr_failure_never_submits_a_password(monkeypatch):
    def fail(_, **_kwargs):
        raise DomainError("provider_captcha_failed", "failed")

    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.recognize_captcha", fail
    )
    paths = []

    def remote(req):
        paths.append(req.url.path)
        return (
            response({"opId": 1000})
            if req.url.path.endswith("/Config")
            else response({"captchaId": "c", "imageBytes": "cG5n"})
        )

    with pytest.raises(DomainError):
        MinglueClient.login(
            httpx.Client(transport=httpx.MockTransport(remote)),
            account="user",
            password="secret",
        )
    assert not any(p.endswith("/Login") for p in paths)


def test_login_deadline_stops_before_any_further_request(monkeypatch):
    clock = iter([0.0, 36.0])
    monkeypatch.setattr(
        "app.modules.providers.adapters.minglue.monotonic", lambda: next(clock)
    )
    calls = []
    with pytest.raises(DomainError) as error:
        MinglueClient.login(
            httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))),
            account="fixture",
            password="fixture",
        )
    assert error.value.code == "provider_unavailable"
    assert calls == []


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"sortNo": 2, "sectionNo": 3}, 2),
        ({"sortNo": None, "sectionNo": 3, "name": "episode"}, 3),
        ({"name": "第8集"}, 8),
    ],
)
def test_section_number_matches_cli_precedence(row, expected):
    assert MinglueClient.section_number(row) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"id": "20"}},
        {"code": False, "data": {}},
        {"code": "unexpected", "data": {}},
    ],
)
def test_malformed_write_envelope_remains_unknown(body):
    with pytest.raises(DomainError) as error:
        client(lambda _: httpx.Response(200, json=body)).create_link("12", CONFIG)
    assert error.value.code == "provider_result_unknown"
