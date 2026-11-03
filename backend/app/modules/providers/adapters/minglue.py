"""明略分销后台协议，按本地 CLI 实现；所有业务请求显式绑定应用。"""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from app.core.errors import DomainError
from app.modules.providers.capabilities import capabilities_for_kind
from app.modules.providers.schemas import DramaCandidate

from .contract import (
    JsonDict,
    LinkLookupPage,
    LinkReceipt,
    SearchPage,
    external_id,
    failure,
    positive,
    request_json,
    verify_receipt,
)

BASE = "https://sp-api.hahip.com/api"
PAGE_SIZE = 100
MAX_PAGES = 50
LOGIN_ATTEMPTS = 2
LOGIN_BUDGET_SECONDS = 35
OCR_TIMEOUT_SECONDS = 15


def recognize_captcha(image: bytes, *, timeout: float = OCR_TIMEOUT_SECONDS) -> str:
    """识别进程有界退出，释放 ONNX 内存；不落盘验证码或读取 CLI 账号文件。"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "captcha_ocr.py"),
            ],
            input=image,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        code = result.stdout.decode().strip()
        if not re.fullmatch(r"[A-Za-z0-9]{3,6}", code):
            raise ValueError
        return code
    except OSError, subprocess.SubprocessError, ValueError, UnicodeError:
        raise failure("provider_captcha_failed") from None


def _page(data: Any, page: int) -> tuple[list[JsonDict], str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise failure("provider_schema_unsupported")
    rows, total = data["items"], data.get("totalItems")
    if (
        type(total) is not int
        or total < 0
        or any(not isinstance(row, dict) for row in rows)
        or len(rows) > PAGE_SIZE
        or total < (page - 1) * PAGE_SIZE + len(rows)
        or len(rows) != min(PAGE_SIZE, max(total - (page - 1) * PAGE_SIZE, 0))
    ):
        raise failure("lookup_incomplete")
    cursor = str(page + 1) if page * PAGE_SIZE < total else None
    if cursor and page >= MAX_PAGES:
        raise failure("lookup_incomplete")
    return rows, cursor


class MinglueClient:
    verify_link = staticmethod(verify_receipt)

    def __init__(
        self,
        http: httpx.Client,
        *,
        token: str = "",
        application_id: str = "",
        channel_config: JsonDict | None = None,
    ):
        self.http, self.token, self.application_id = http, token, application_id
        self.channel_config = dict(channel_config or {})
        self.login_deadline: float | None = None

    @classmethod
    def login(cls, http: httpx.Client, *, account: str, password: str) -> MinglueClient:
        client = cls(http)
        # 首次加载 OCR 动态库可能较慢，整次握手仍须在 Worker 硬限之前主动结束。
        client.login_deadline = monotonic() + LOGIN_BUDGET_SECONDS
        config = client._request(
            "GET", "/Home/Config", query={"domain": "sp.hahip.com"}
        )
        if not isinstance(config, dict):
            raise failure("provider_schema_unsupported")
        op_id = positive(config.get("opId"))
        for attempt in range(LOGIN_ATTEMPTS):
            ticks = 621355968000000000 + int(datetime.now(UTC).timestamp() * 10_000_000)
            captcha = client._request("GET", "/Home/Captcha", query={"t": str(ticks)})
            if not isinstance(captcha, dict) or not captcha.get("captchaId"):
                raise failure("provider_schema_unsupported")
            try:
                image = base64.b64decode(captcha["imageBytes"], validate=True)
            except KeyError, ValueError, TypeError, binascii.Error:
                raise failure("provider_schema_unsupported") from None
            if not image or len(image) > 1024 * 1024:
                raise failure("provider_schema_unsupported")
            try:
                code = recognize_captcha(
                    image, timeout=client._timeout(OCR_TIMEOUT_SECONDS)
                )
            except DomainError as error:
                if (
                    error.code != "provider_captcha_failed"
                    or attempt == LOGIN_ATTEMPTS - 1
                ):
                    raise
                continue
            # 验证码错误可以重新取图；账号错误明确终止，不进入无限自动重登。
            body = client._body(
                "POST",
                "/Home/Login",
                json={
                    "loginName": account,
                    "loginPass": password,
                    "captchaCode": code,
                    "captchaCodeKey": captcha["captchaId"],
                    "opId": op_id,
                    "scene": 1,
                },
            )
            if type(body.get("code")) is not int:
                raise failure("provider_schema_unsupported")
            if body["code"] == 0:
                token = body.get("data")
                if not isinstance(token, str) or not token:
                    raise failure("provider_schema_unsupported")
                client.token = token
                client.login_deadline = None
                return client
            if "验证码" not in str(body.get("msg", "")):
                raise failure("provider_auth_failed")
        raise failure("provider_captcha_failed")

    def _timeout(self, maximum: float) -> float:
        if self.login_deadline is None:
            return maximum
        remaining = self.login_deadline - monotonic()
        if remaining <= 0:
            raise failure("provider_unavailable", retryable=True)
        return min(maximum, remaining)

    def _body(
        self,
        method: str,
        path: str,
        *,
        query: JsonDict | None = None,
        json: JsonDict | None = None,
        write: bool = False,
    ) -> JsonDict:
        # 复用传输边界的日志脱敏和未知写入分类，登录请求采用较短超时。
        body, _ = request_json(
            self.http,
            method,
            BASE + path,
            params=query,
            json=json,
            headers={"Authorization": self.token} if self.token else {},
            write=write,
            timeout=self._timeout(5) if self.login_deadline is not None else 30,
        )
        return body

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: JsonDict | None = None,
        json: JsonDict | None = None,
        write: bool = False,
    ) -> Any:
        body = self._body(method, path, query=query, json=json, write=write)
        if type(body.get("code")) is not int:
            raise failure(
                "provider_result_unknown" if write else "provider_schema_unsupported"
            )
        if body["code"] == 401:
            raise failure("provider_session_expired")
        if body.get("code") != 0:
            raise failure("provider_rejected")
        if "data" not in body:
            raise failure(
                "provider_result_unknown" if write else "provider_schema_unsupported"
            )
        return body["data"]

    def _context(self) -> JsonDict:
        if not self.application_id or self.channel_config.get("package_type") != 4:
            raise failure("provider_application_forbidden")
        return {
            "partnerId": external_id(self.channel_config.get("partner_id")),
            "appId": self.application_id,
        }

    def discover_applications(self) -> list[JsonDict]:
        result, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            data = self._request(
                "GET", "/App/List", query={"PageIndex": page, "PageSize": PAGE_SIZE}
            )
            rows, cursor = _page(data, page)
            for row in rows:
                if row.get("packageType") != 4:
                    continue
                app_id, partner_id = (
                    external_id(row.get("id")),
                    external_id(row.get("partnerId")),
                )
                name = row.get("name")
                if app_id in seen or not isinstance(name, str) or not name.strip():
                    raise failure("provider_schema_unsupported")
                seen.add(app_id)
                result.append(
                    {
                        "external_id": app_id,
                        "name": name,
                        "channel_config": {
                            "partner_id": partner_id,
                            "package_type": 4,
                            "content_type": positive(row.get("contentType")),
                        },
                        # 内部 appId 不是 TikTok Minis ID；搭建页沿用同名匹配及用户选择流程。
                        "tiktok_minis_id": None,
                    }
                )
            if cursor is None:
                if not result:
                    raise failure("provider_application_discovery_unverified")
                return result
        raise failure("lookup_incomplete")

    def search(self, title: str, cursor: str | None) -> SearchPage:
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/Content/List",
            query={
                **self._context(),
                "contentName": title,
                "contentType": positive(self.channel_config.get("content_type")),
                "finishStatus": -1,
                "feeType": -1,
                "pageIndex": page,
                "pageSize": PAGE_SIZE,
            },
        )
        rows, next_cursor = _page(data, page)
        items = []
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str):
                raise failure("provider_schema_unsupported")
            if name == title:
                drama_id = external_id(row.get("id"))
                # feeSectionStart 是应用默认值，不能当作逐剧事实。
                items.append(
                    DramaCandidate(
                        external_drama_id=drama_id,
                        display_drama_id=drama_id,
                        title=name,
                    )
                )
        return SearchPage(
            items=items, next_cursor=next_cursor, complete=next_cursor is None
        )

    def check_visibility(self, drama_id: str) -> None:
        data = self._request(
            "GET",
            "/Content/CheckPromotionVisibility",
            query={**self._context(), "contentId": drama_id},
        )
        if not isinstance(data, dict) or type(data.get("visible")) is not bool:
            raise failure("provider_schema_unsupported")
        if data["visible"] is not True:
            raise failure("provider_application_forbidden")

    def section_page(
        self, drama_id: str, cursor: str | None
    ) -> tuple[list[JsonDict], str | None]:
        page = positive(cursor or 1)
        return _page(
            self._request(
                "GET",
                "/Section/List",
                query={
                    **self._context(),
                    "contentId": drama_id,
                    "pageIndex": page,
                    "pageSize": PAGE_SIZE,
                },
            ),
            page,
        )

    @staticmethod
    def section_number(row: JsonDict) -> int:
        value = row.get("sortNo")
        if value is None:
            value = row.get("sectionNo")
        if value is None:
            match = re.search(r"\d+", str(row.get("name", "")))
            value = match.group() if match else None
        return positive(value)

    def lookup_link(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> LinkLookupPage:
        self._validate_config(drama_id, config)
        page = positive(cursor or 1)
        rows, next_cursor = _page(
            self._request(
                "GET",
                "/Promotion/List",
                query={
                    **self._context(),
                    "pageIndex": page,
                    "pageSize": PAGE_SIZE,
                },
            ),
            page,
        )
        matched = []
        for row in rows:
            if (
                str(row.get("contentId")) == drama_id
                and str(row.get("sectionId")) == config["section_id"]
                and str(row.get("appId", self.application_id)) == self.application_id
                and str(row.get("partnerId", self.channel_config["partner_id"]))
                == self.channel_config["partner_id"]
            ):
                external_id(row.get("promotionId", row.get("id")))
                matched.append(row)
        return LinkLookupPage(
            items=matched, next_cursor=next_cursor, complete=next_cursor is None
        )

    def _validate_config(self, drama_id: str, config: JsonDict) -> None:
        context = self._context()
        if (
            config.get("app_id") != context["appId"]
            or config.get("partner_id") != context["partnerId"]
            or config.get("content_id") != drama_id
            or config.get("content_type") != self.channel_config.get("content_type")
        ):
            raise failure("provider_request_invalid")
        external_id(config.get("section_id"))
        positive(config.get("episode"))

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        self._validate_config(drama_id, config)
        section_id = config["section_id"]
        data = self._request(
            "POST",
            "/Promotion/Save",
            write=True,
            json={
                **self._context(),
                "name": config.get("name", f"{drama_id}_{datetime.now(UTC):%m%d}"),
                "contentId": drama_id,
                "sectionId": section_id,
                "contentType": config["content_type"],
                "iapStrategyGroupId": "",
                "coinPerSection": "",
                "feeSectionStart": "",
                "forceSubscribe": 0,
                "forceSubscribeSectionId": section_id,
                "type": 0,
                "pixelCode": "",
                "adIds": "",
            },
        )
        if not isinstance(data, dict):
            raise failure("provider_result_unknown")
        return self._receipt(data, config)

    def read_link(self, remote_id: str) -> LinkReceipt:
        data = self._request(
            "GET",
            "/Promotion/Detail",
            query={**self._context(), "promotionId": external_id(remote_id)},
        )
        if not isinstance(data, dict):
            raise failure("provider_result_unknown")
        # 身份必须由详情证明；不能用请求参数填补缺失的应用/剧集归属。
        if (
            str(data.get("promotionId", data.get("id"))) != remote_id
            or str(data.get("appId")) != self.application_id
            or str(data.get("partnerId")) != self.channel_config.get("partner_id")
        ):
            raise failure("config_unverifiable")
        config = {
            "partner_id": external_id(data.get("partnerId")),
            "app_id": external_id(data.get("appId")),
            "content_id": external_id(data.get("contentId")),
            "section_id": external_id(data.get("sectionId")),
        }
        return self._receipt(data, config)

    @staticmethod
    def _receipt(data: JsonDict, config: JsonDict) -> LinkReceipt:
        url = data.get("tkMinisUrl")
        if not isinstance(url, str) or not url:
            raise failure("provider_result_unknown")
        try:
            return LinkReceipt(
                remote_id=external_id(data.get("promotionId", data.get("id"))),
                url=url,
                protected_base="",
                attribution={
                    key: data[key]
                    for key in (
                        "partnerId",
                        "appId",
                        "contentId",
                        "sectionId",
                        "pagePath",
                        "name",
                    )
                    if key in data
                },
                config=config,
            )
        except DomainError, ValueError, TypeError:
            raise failure("provider_result_unknown") from None

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        from dataclasses import asdict

        if step != "create" or not isinstance(payload.get("config"), dict):
            raise failure("provider_request_invalid")
        return asdict(self.create_link(str(payload.get("vid")), payload["config"]))

    def capabilities(self, application: Any = None):
        return capabilities_for_kind("minglue", application)
