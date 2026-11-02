"""容量分销后台 adapter with strict post-create batch read-back."""

from __future__ import annotations

import json as jsonlib
import os
import secrets
from typing import Any, Literal
from urllib.parse import urlencode

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
    string,
    verify_receipt,
)

AUTH_BASE = "https://distribution.wolftv.online/manage/ocean/management/distribution"
API_BASE = f"{AUTH_BASE}/management"
PAGE_SIZE = 100
COOKIE_DOMAIN = "distribution.wolftv.online"
XLA_CI = os.getenv("RONG_LIANG_XLA_CI", "").strip()
# 容量登录失效时返回码可能是 user-2/401/403；实测未登录是 HTTP 500 + code=403，
# 所以必须先看返回码再看 HTTP 状态，否则会被当成“暂不可用”而不触发自动重登。
SESSION_EXPIRED_CODES = {"user-2", 401, "401", 403, "403"}


def _checked_body(status: int, body: Any, *, write: bool) -> Any:
    if status in {401, 403} or (
        isinstance(body, dict) and body.get("code") in SESSION_EXPIRED_CODES
    ):
        raise failure("provider_session_expired")
    if status >= 500 or status in {408, 429}:
        raise failure(
            "provider_result_unknown" if write else "provider_unavailable",
            retryable=not write,
        )
    if not 200 <= status < 300:
        raise failure("provider_rejected")
    # 容量接口不统一包裹 code/data：剧集返回数组，建链成功可直接返回 true。
    # 写入后的未知响应不能判为明确失败，否则会跳过已有批次的只读恢复。
    if not isinstance(body, dict | list) and not (write and body is True):
        raise failure("provider_result_unknown" if write else "provider_schema_unsupported")
    return body


class _CurlSession:
    """Small cookie-preserving libcurl session for the capacity backend.

    The provider accepts the browser/libcurl TLS session but intermittently
    rejects the equivalent HTTPX session as unauthenticated.  Keep this
    transport local to this adapter; tests continue to use the HTTPX path.
    """

    def __init__(self, *, cookie: str = ""):
        try:
            from curl_cffi import CurlMime, requests
        except ImportError:
            raise failure("provider_unavailable", retryable=True) from None
        self._curl_mime = CurlMime
        self.session = requests.Session(impersonate="chrome")
        self.xla_ci = XLA_CI or secrets.token_hex(16)
        self.session.cookies.set("XLA_CI", self.xla_ci, domain=COOKIE_DOMAIN, path="/")
        if cookie:
            self.session.headers["Cookie"] = cookie

    def close(self) -> None:
        self.session.close()

    def __del__(self) -> None:
        self.close()

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: JsonDict | None = None,
        json: JsonDict | None = None,
        content: str | None = None,
        form: dict[str, str] | None = None,
        write: bool = False,
    ) -> Any:
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        request_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
        }
        request_headers.update(headers or {})
        body = content
        multipart = None
        if form:
            multipart = self._curl_mime()
            for name, value in form.items():
                multipart.addpart(name=name, data=value.encode())
            body = None
        try:
            response = self.session.request(
                method,
                url,
                headers=request_headers,
                data=body,
                json=json,
                multipart=multipart,
                timeout=30,
                allow_redirects=False,
            )
        except Exception:
            raise failure(
                "provider_result_unknown" if write else "provider_unavailable",
                retryable=not write,
            ) from None
        try:
            status = response.status_code
            body_value = response.json()
        except (TypeError, ValueError, jsonlib.JSONDecodeError):
            raise failure("provider_result_unknown" if write else "provider_schema_unsupported") from None
        return _checked_body(status, body_value, write=write)


class RongliangClient:
    verify_link = staticmethod(verify_receipt)

    def __init__(
        self,
        http: httpx.Client,
        *,
        token: str = "",
        curl_session: _CurlSession | None = None,
    ):
        self.http, self.token, self._curl = http, token, curl_session
        # 容量网页依赖登录响应下发的完整 Cookie 集合。旧实现手工只发
        # dist_token，会丢掉同一登录会话的其他 Cookie，导致后台返回
        # HTTP 500 / code=403 (admin not login)。恢复已缓存 token 时也要
        # 把 token 注入同一个 httpx Cookie jar，保持首次登录和自动重登一致。
        if token and not any(cookie.name == "dist_token" for cookie in self.http.cookies.jar):
            self.http.cookies.set("dist_token", token, domain=COOKIE_DOMAIN, path="/")

    def __del__(self) -> None:
        if self._curl is not None:
            self._curl.close()

    @classmethod
    def login(
        cls,
        http: httpx.Client,
        *,
        email: str,
        password: str,
        use_curl: bool = False,
    ) -> RongliangClient:
        encoded = urlencode({"email": email, "password": password})
        curl: _CurlSession | None = _CurlSession() if use_curl else None
        try:
            if curl is None:
                body, _ = request_json(
                    http,
                    "POST",
                    AUTH_BASE + "/auth/login",
                    headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    content=encoded,
                )
            else:
                body = curl.request(
                    "POST",
                    AUTH_BASE + "/auth/login",
                    form={"email": email, "password": password},
                )
        except Exception:
            if curl is not None:
                curl.close()
            raise
        # 登录成功响应当前没有 code 字段，只有 token 和用户信息；与 CLI
        # 及浏览器实际响应保持一致，只有明确的非零 code 才视为认证失败。
        if body.get("code") not in (None, 0, "0") or not isinstance(body.get("token"), str):
            raise failure("provider_auth_failed")
        return cls(http, token=body["token"], curl_session=curl)

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        query: JsonDict | None = None,
        json: JsonDict | None = None,
        write: bool = False,
    ) -> Any:
        if self._curl is not None:
            body = self._curl.request(method, API_BASE + path, params=query, json=json, write=write)
        else:
            # 不走通用 request_json：它遇到 HTTP 500 会丢掉响应体，
            # 看不到容量“未登录”的 code=403。
            try:
                response = self.http.request(
                    method,
                    API_BASE + path,
                    params=query,
                    json=json,
                    timeout=30,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                raise failure(
                    "provider_result_unknown" if write else "provider_unavailable",
                    retryable=not write,
                ) from None
            try:
                body_value = response.json()
            except ValueError:
                body_value = None
            if body_value is None and 200 <= response.status_code < 300:
                raise failure(
                    "provider_result_unknown" if write else "provider_schema_unsupported"
                )
            body = _checked_body(response.status_code, body_value, write=write)
        if isinstance(body, list):
            return body
        if write and body is True:
            return True
        code = body.get("code")
        if code not in (None, 0, "0"):
            raise failure("provider_rejected")
        return body.get("data", body)

    def discover_applications(self) -> list[JsonDict]:
        data = self._request("GET", "/link/form_data")
        options = data.get("packageOptions") if isinstance(data, dict) else None
        if not isinstance(options, list):
            raise failure("provider_application_discovery_unverified")
        result: list[JsonDict] = []
        seen: set[str] = set()
        for row in options:
            if not isinstance(row, dict) or row.get("clientType") != "TIKTOK_MINI_PROGRAM":
                continue
            client_id = external_id(row.get("clientId"))
            name = row.get("clientName")
            if client_id in seen or not isinstance(name, str) or not name.strip():
                raise failure("provider_schema_unsupported")
            seen.add(client_id)
            result.append(
                {
                    "external_id": client_id,
                    "name": name,
                    "channel_config": {"platform": 1},
                    "tiktok_minis_id": None,
                }
            )
        if not result:
            raise failure("provider_application_discovery_unverified")
        return result

    def search(self, title: str, cursor: str | None) -> SearchPage:
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/compilations/page",
            query={"pageNumber": page, "pageSize": PAGE_SIZE, "compilationsName": title},
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_schema_unsupported")
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        items: list[DramaCandidate] = []
        for row in data["records"]:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            titles = (row.get("compilationsName"), row.get("originalTitle"))
            if not any(isinstance(value, str) for value in titles):
                raise failure("provider_schema_unsupported")
            # 容量当前剧名可能已改名，不能因原名存在而丢弃当前剧名的精确匹配。
            # 两个名称任一完全匹配即可；保留全部同名候选，交给准备流程消歧。
            if title in titles:
                items.append(
                    DramaCandidate(
                        external_drama_id=external_id(row.get("compilationsId")),
                        display_drama_id=external_id(row.get("compilationsId")),
                        title=title,
                        language=row.get("languageName") if isinstance(row.get("languageName"), str) else None,
                    )
                )
        complete = page * PAGE_SIZE >= total
        return SearchPage(items=items, next_cursor=None if complete else str(page + 1), complete=complete)

    def episode_options(self, compilation_id: str) -> list[JsonDict]:
        data = self._request(
            "GET",
            "/link/episodic_dramas",
            query={"compilationsId": external_id(compilation_id)},
        )
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise failure("provider_schema_unsupported")
        return data

    def compilations_alias(self, compilation_id: str) -> str:
        """与后台新增链接表单一致：取第一个别名，没有别名用原名；别名决定计划名剧名部分。"""

        data = self._request(
            "GET", "/link/compilations_alias", query={"compilationsId": external_id(compilation_id)}
        )
        if not isinstance(data, dict):
            raise failure("provider_schema_unsupported")
        # 后台规则 aliasList[0] || originalTitle（JS 真值判断）：空串视为缺失，
        # 两者都没有时不能传空别名，否则计划名剧名部分为空
        aliases = data.get("aliasList")
        if isinstance(aliases, list) and aliases:
            first = aliases[0]
            if isinstance(first, str) and first.strip():
                return first
        title = data.get("originalTitle")
        if isinstance(title, str) and title.strip():
            return title
        raise failure("provider_schema_unsupported")

    def lookup_link(self, drama_id: str, config: JsonDict, cursor: str | None) -> LinkLookupPage:
        normalized = self._validate_config(config, require_episode=True)
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/link/page",
            query={
                "pageNumber": page,
                "pageSize": PAGE_SIZE,
                "clientId": normalized["client_id"],
                "deliverPlatform": normalized["platform"],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_schema_unsupported")
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        matched: list[JsonDict] = []
        for row in data["records"]:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            if self._matches(row, drama_id, normalized):
                detail = dict(row)
                batch = detail.get("batchId")
                if not isinstance(batch, str) or not batch:
                    raise failure("provider_schema_unsupported")
                detail.update(self._link_url(batch))
                matched.append(detail)
        complete = page * PAGE_SIZE >= total
        return LinkLookupPage(items=matched, next_cursor=None if complete else str(page + 1), complete=complete)

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        normalized = self._validate_config(config, require_episode=True)
        body = {
            "clientId": normalized["client_id"],
            "deliverPlatform": normalized["platform"],
            "deliveryType": normalized["delivery_type"],
            "linkType": normalized["link_type"],
            "compilationsId": positive(drama_id),
            "compilationsAlias": normalized.get("alias", ""),
            "episodicDramaId": normalized["episodic_drama_id"],
        }
        try:
            result = self._request("POST", "/link/create", json=body, write=True)
        except DomainError as error:
            if error.code not in {
                "provider_rejected", "provider_result_unknown",
                "provider_schema_unsupported", "provider_unavailable",
            }:
                raise
            # 重复/异常回执不代表未创建。按本次应用、剧、起播集和平台精确回读，
            # 有已存在批次直接复用；查无结果才保留原错误，绝不再次发送创建请求。
            receipt = self._find_created_link(drama_id, normalized)
            if receipt is not None:
                return receipt
            raise
        if result is not True and not (isinstance(result, dict) and result.get("success") is True):
            raise failure("provider_result_unknown")
        receipt = self._find_created_link(drama_id, normalized)
        if receipt is None:
            raise failure("provider_result_unknown")
        return receipt

    def _find_created_link(self, drama_id: str, config: JsonDict) -> LinkReceipt | None:
        page = self.lookup_link(drama_id, config, None)
        # 即时回读仍有界；目录多页时交给原 recover_create 持久分页继续，
        # 不能把只查首页当成“链接不存在”，也不在单个 Worker 步骤内无限扫描。
        if not page.complete:
            raise failure("provider_result_unknown")
        found = {row["batchId"]: row for row in page.items}
        if len(found) > 1:
            raise failure("provider_result_unknown")
        if not found:
            return None
        batch, data = next(iter(found.items()))
        return self._receipt(batch, data, config)

    def read_link(self, remote_id: str) -> LinkReceipt:
        batch_id = external_id(remote_id)
        data = self._request(
            "GET",
            "/link/page",
            query={
                "pageNumber": 1,
                "pageSize": PAGE_SIZE,
                "batchId": batch_id,
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_result_unknown")
        rows = [
            row
            for row in data["records"]
            if isinstance(row, dict) and str(row.get("batchId")) == batch_id
        ]
        if len(rows) != 1:
            raise failure("provider_result_unknown")
        row = rows[0]
        config: JsonDict = {
            "client_id": positive(row.get("clientId")),
            "episodic_drama_id": positive(row.get("episodicDramaId")),
            "platform": positive(row.get("deliverPlatform", 1)),
            "delivery_type": positive(row.get("deliveryType", 1)),
            "link_type": positive(row.get("linkType", 2)),
        }
        if isinstance(row.get("compilationsAlias"), str):
            config["alias"] = row["compilationsAlias"]
        url_data = self._link_url(batch_id)
        return self._receipt(batch_id, url_data, config)

    def capabilities(self, application: Any = None):
        return capabilities_for_kind("rongliang", application)

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        if step != "create" or not isinstance(payload.get("config"), dict):
            raise failure("provider_request_invalid")
        receipt = self.create_link(str(payload.get("vid")), payload["config"])
        return {
            "remote_id": receipt.remote_id,
            "url": receipt.url,
            "protected_base": receipt.protected_base,
            "attribution": receipt.attribution,
            "config": receipt.config,
        }

    def _link_url(self, batch_id: str) -> JsonDict:
        data = self._request("GET", "/link/url", query={"batchId": batch_id})
        if not isinstance(data, dict) or not isinstance(data.get("deepLink"), str) or not data["deepLink"]:
            raise failure("provider_result_unknown")
        return data

    @staticmethod
    def _matches(row: JsonDict, drama_id: str, config: JsonDict) -> bool:
        return (
            str(row.get("compilationsId")) == str(drama_id)
            and str(row.get("episodicDramaId")) == str(config["episodic_drama_id"])
            and str(row.get("clientId")) == str(config["client_id"])
            and str(row.get("deliverPlatform")) == str(config["platform"])
        )

    @staticmethod
    def _validate_config(config: JsonDict, *, require_episode: bool) -> JsonDict:
        allowed = {
            "client_id",
            "episodic_drama_id",
            "platform",
            "delivery_type",
            "link_type",
            "alias",
        }
        if not isinstance(config, dict) or set(config) - allowed:
            raise failure("provider_request_invalid")
        try:
            normalized = {
                "client_id": positive(config["client_id"]),
                "platform": positive(config.get("platform", 1)),
                "delivery_type": positive(config.get("delivery_type", 1)),
                "link_type": positive(config.get("link_type", 2)),
            }
            if require_episode:
                normalized["episodic_drama_id"] = positive(config["episodic_drama_id"])
        except (KeyError, TypeError, ValueError):
            raise failure("provider_request_invalid") from None
        if config.get("alias") is not None:
            normalized["alias"] = string(config["alias"]).strip()
        return normalized

    @staticmethod
    def _receipt(remote_id: str, data: JsonDict, config: JsonDict) -> LinkReceipt:
        try:
            return LinkReceipt(
                remote_id=remote_id,
                url=string(data["deepLink"]),
                protected_base=string(data.get("planName") or data.get("adGroupName") or ""),
                attribution={
                    "planName": data.get("planName") or "",
                    "adGroupName": data.get("adGroupName") or "",
                },
                config=config,
            )
        except (KeyError, ValueError):
            raise failure("provider_result_unknown") from None
