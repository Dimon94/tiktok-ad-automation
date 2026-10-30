"""SDK自有连接的阶段摘要；不复制网络发送实现，不采集URL/头部/正文。"""

import ipaddress
import math
import os
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import uuid4

import urllib3
from billiard.exceptions import SoftTimeLimitExceeded
from urllib3.exceptions import HTTPError

STAGES = {
    "unknown",
    "connection_validation",
    "connection_setup",
    "dns_tcp_connect",
    "tls_setup",
    "proxy_tunnel",
    "request_write",
    "response_wait",
    "response_read",
    "complete",
}
SEND_STATES = {"unknown", "not_sent", "may_have_sent", "write_completed"}


def observe_transport(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """诊断失败不能替换业务返回/异常，Worker中断仍按原生命周期传播。"""
    try:
        return callback(*args, **kwargs)
    except SoftTimeLimitExceeded, KeyboardInterrupt, SystemExit:
        raise
    except Exception:
        return None


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, str]:
    revision = os.getenv("TK_ADA_BUILD_REVISION", "unknown")
    result = {"urllib3_version": urllib3.__version__}
    if revision in {"local", "unknown"} or re.fullmatch(r"[0-9a-f]{40}", revision):
        result["build_revision"] = revision
    pod = os.getenv("HOSTNAME", "")
    if re.fullmatch(r"[a-zA-Z0-9_.-]{1,253}", pod):
        result["pod"] = pod
    try:
        result["transport_source_sha256"] = sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
    except OSError:
        pass
    return result


def _number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 86_400_000
    except OverflowError:
        return False


def _safe_transport_summary(value: object) -> dict[str, Any]:
    """数据库和日志共用固定白名单，调用者额外夹带的内容不能持久化。"""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, allowed in (("failure_stage", STAGES), ("send_state", SEND_STATES)):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    identity = value.get("http_attempt_id")
    if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{32}", identity):
        result["http_attempt_id"] = identity
    for key in ("connect_timeout_seconds", "read_timeout_seconds"):
        if _number(value.get(key)) and value[key] <= 3600:
            result[key] = value[key]
    for key in (
        "request_started",
        "request_write_completed",
        "response_started",
        "response_received",
        "connection_reused",
    ):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if isinstance(value.get("phase_ms"), dict):
        result["phase_ms"] = {
            k: round(value["phase_ms"][k], 3)
            for k in STAGES
            if _number(value["phase_ms"].get(k))
        }
    for key, lower, upper in (
        ("peer_port", 1, 65535),
        ("errno", -65535, 65535),
        ("http_status", 100, 599),
    ):
        if type(value.get(key)) is int and lower <= value[key] <= upper:
            result[key] = value[key]
    error_type = value.get("error_type")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", error_type):
        result["error_type"] = error_type
    frames = value.get("error_frames")
    if isinstance(frames, list):
        result["error_frames"] = [
            s
            for s in frames[:8]
            if isinstance(s, str)
            and len(s) <= 220
            and re.fullmatch(r"[A-Za-z0-9_.-]+:[0-9]+:[A-Za-z0-9_<>.-]+", s)
        ]
    try:
        if (
            isinstance(value.get("peer_ip"), str)
            and len(value["peer_ip"]) <= 45
            and "%" not in value["peer_ip"]
        ):
            result["peer_ip"] = str(ipaddress.ip_address(value["peer_ip"]))
    except ValueError:
        pass
    if isinstance(value.get("tls_version"), str) and value["tls_version"] in {
        "TLSv1",
        "TLSv1.1",
        "TLSv1.2",
        "TLSv1.3",
    }:
        result["tls_version"] = value["tls_version"]
    if isinstance(value.get("runtime"), dict):
        runtime = value["runtime"]
        patterns = {
            "pod": r"[a-zA-Z0-9_.-]{1,253}",
            "build_revision": r"(?:[0-9a-f]{40}|local|unknown)",
            "urllib3_version": r"[0-9][a-zA-Z0-9_.-]{0,31}",
            "transport_source_sha256": r"[0-9a-f]{64}",
        }
        result["runtime"] = {
            k: runtime[k]
            for k, pattern in patterns.items()
            if isinstance(runtime.get(k), str) and re.fullmatch(pattern, runtime[k])
        }
    return result


def safe_transport_summary(value: object) -> dict[str, Any]:
    result = observe_transport(_safe_transport_summary, value)
    return result if isinstance(result, dict) else {}


def transport_summary(trace: object) -> dict[str, Any]:
    if not isinstance(trace, TransportTrace):
        return {}
    return safe_transport_summary(observe_transport(trace.summary))


@dataclass
class TransportTrace:
    marker: object
    http_attempt_id: str = field(default_factory=lambda: uuid4().hex)
    phase: str = "unknown"
    failure_stage: str | None = None
    phase_ms: dict[str, float] = field(default_factory=dict)
    connection_started: bool = False
    tcp_ready: bool = False
    pre_http_failure: bool = False
    request_started: bool = False
    request_write_completed: bool = False
    response_started: bool = False
    response_received: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def fail(self, error: BaseException) -> None:
        self.failure_stage = self.failure_stage or self.phase
        if "error_type" not in self.details:
            self.details["error_type"] = type(error).__name__
            self.details["error_frames"] = [
                f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                for f in traceback.extract_tb(error.__traceback__)[-8:]
            ]
        number = getattr(error, "errno", None)
        if type(number) is int:
            self.details["errno"] = number

    def elapsed(self, phase: str, started: float) -> None:
        self.phase_ms[phase] = (
            self.phase_ms.get(phase, 0) + (monotonic() - started) * 1000
        )

    def summary(self) -> dict[str, Any]:
        send_state = (
            "not_sent"
            if self.pre_http_failure and not self.request_started
            else "write_completed"
            if self.request_write_completed
            else "may_have_sent"
            if self.request_started
            else "unknown"
        )
        result = {
            "http_attempt_id": self.http_attempt_id,
            "failure_stage": self.failure_stage,
            "send_state": send_state,
            "phase_ms": self.phase_ms,
            "request_started": self.request_started,
            "request_write_completed": self.request_write_completed,
            "response_started": self.response_started,
            "response_received": self.response_received,
            **self.details,
        }
        if self.request_started:
            result["connection_reused"] = not self.connection_started
        if self.failure_stage:
            result["runtime"] = runtime_identity()
        return safe_transport_summary(result)


def begin_transport_trace(client: Any, timeout: object) -> TransportTrace:
    state = getattr(client, "_tiktok_connection_setup_state", None)
    if isinstance(state, dict):
        state["transport_trace"] = None
    trace = TransportTrace(
        marker=state["marker"] if isinstance(state, dict) else object()
    )
    if isinstance(timeout, tuple) and len(timeout) == 2:
        trace.details.update(
            connect_timeout_seconds=timeout[0], read_timeout_seconds=timeout[1]
        )
    if isinstance(state, dict):
        state["transport_trace"] = trace
    return trace


def _transport_snapshot(
    client: Any, *, include_runtime: bool = False
) -> dict[str, Any]:
    state = getattr(client, "_tiktok_connection_setup_state", None)
    if not isinstance(state, dict):
        return {}
    trace = state.get("transport_trace")
    if not isinstance(trace, TransportTrace) or trace.marker is not state["marker"]:
        return {}
    summary = transport_summary(trace)
    if include_runtime:
        summary["runtime"] = observe_transport(runtime_identity)
    return safe_transport_summary(summary)


def transport_snapshot(client: Any, *, include_runtime: bool = False) -> dict[str, Any]:
    result = observe_transport(
        _transport_snapshot, client, include_runtime=include_runtime
    )
    return result if isinstance(result, dict) else {}


def _peer(trace: TransportTrace, sock: Any) -> None:
    try:
        peer = sock.getpeername()
        trace.details.update(
            peer_ip=str(ipaddress.ip_address(peer[0])), peer_port=peer[1]
        )
        if callable(getattr(sock, "version", None)):
            trace.details["tls_version"] = sock.version()
    except OSError, AttributeError, TypeError, ValueError:
        pass


def install_transport_tracking(client: Any) -> None:
    state = {"marker": object(), "transport_trace": None}
    client._tiktok_connection_setup_state = state
    manager = client.rest_client.pool_manager
    base_pool = manager.pool_classes_by_scheme["https"]
    base_connection = base_pool.ConnectionCls

    def active() -> TransportTrace | None:
        trace = state["transport_trace"]
        return (
            trace
            if isinstance(trace, TransportTrace) and trace.marker is state["marker"]
            else None
        )

    class TrackedConnection(base_connection):
        def _new_conn(self):
            trace = active()
            if trace is None:
                return super()._new_conn()
            trace.connection_started, trace.phase = True, "dns_tcp_connect"
            started = monotonic()
            try:
                sock = super()._new_conn()
            except BaseException as error:
                observe_transport(trace.fail, error)
                raise
            finally:
                observe_transport(trace.elapsed, "dns_tcp_connect", started)
            trace.tcp_ready, trace.phase = True, "tls_setup"
            observe_transport(_peer, trace, sock)
            return sock

        def connect(self):
            trace = active()
            if trace is None:
                return super().connect()
            trace.connection_started, trace.phase = True, "connection_setup"
            started = monotonic()
            try:
                result = super().connect()
            except BaseException as error:
                observe_transport(trace.fail, error)
                raise
            else:
                observe_transport(_peer, trace, self.sock)
                return result
            finally:
                observe_transport(trace.elapsed, "connection_setup", started)
                if (
                    trace.tcp_ready
                    and "connection_setup" in trace.phase_ms
                    and "dns_tcp_connect" in trace.phase_ms
                ):
                    trace.phase_ms["tls_setup"] = max(
                        0,
                        trace.phase_ms["connection_setup"]
                        - trace.phase_ms.get("dns_tcp_connect", 0)
                        - trace.phase_ms.get("proxy_tunnel", 0),
                    )

        def _tunnel(self):
            trace = active()
            if trace is None:
                return super()._tunnel()
            trace.phase = "proxy_tunnel"
            started = monotonic()
            try:
                return super()._tunnel()
            except BaseException as error:
                observe_transport(trace.fail, error)
                raise
            finally:
                observe_transport(trace.elapsed, "proxy_tunnel", started)
                trace.phase = "tls_setup"

        def request(self, *args, **kwargs):
            cost = getattr(client, "_sync_attempt_observer", None)
            if cost is not None:
                observer, endpoint = cost
                observer.attempt(endpoint)
            trace = active()
            if trace is None:
                return super().request(*args, **kwargs)
            trace.request_started, trace.phase = True, "request_write"
            if self.sock is not None:
                observe_transport(_peer, trace, self.sock)
            started = monotonic()
            try:
                result = super().request(*args, **kwargs)
            except BaseException as error:
                observe_transport(trace.fail, error)
                raise
            finally:
                observe_transport(trace.elapsed, "request_write", started)
            trace.request_write_completed, trace.phase = True, "response_wait"
            return result

        def getresponse(self):
            trace = active()
            if trace is None:
                return super().getresponse()
            trace.response_started, trace.phase = True, "response_read"
            started = monotonic()
            try:
                response = super().getresponse()
            except BaseException as error:
                observe_transport(trace.fail, error)
                raise
            finally:
                observe_transport(trace.elapsed, "response_read", started)
            trace.response_received, trace.phase = True, "complete"
            trace.details["http_status"] = response.status
            return response

    class TrackedPool(base_pool):
        ConnectionCls = TrackedConnection

        def _validate_conn(self, conn):
            trace = active()
            try:
                super()._validate_conn(conn)
            except (HTTPError, OSError) as error:
                # 此边界在HTTP请求之前；原异常类型保持，不能按名字猜发送状态。
                cast(Any, error)._tiktok_before_http = state["marker"]
                if trace is not None:
                    trace.pre_http_failure = True
                    if trace.phase == "unknown":
                        trace.phase = "connection_validation"
                    observe_transport(trace.fail, error)
                raise

    # urllib3默认映射为全局对象，只改当前客户端自己的副本。
    manager.pool_classes_by_scheme = {
        **manager.pool_classes_by_scheme,
        "https": TrackedPool,
    }
