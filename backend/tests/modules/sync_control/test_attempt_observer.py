"""发送观察使用现有SDK连接边界，包含SDK线程且不把本地拒绝当成HTTP。"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.integrations.tiktok.attempt_observer import (
    bind_attempt_observer,
    physical_attempt_scope,
)
from app.integrations.tiktok.transport_diagnostics import install_transport_tracking


def test_actual_transport_attempts_cross_sdk_thread_without_new_sends():
    physical = []
    observed = []

    class Connection:
        def request(self, value):
            physical.append(value)
            if value == "429":
                raise RuntimeError("provider 429")
            return value

    class Pool:
        ConnectionCls = Connection

    client = SimpleNamespace(
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(pool_classes_by_scheme={"https": Pool})
        )
    )
    install_transport_tracking(client)
    connection = client.rest_client.pool_manager.pool_classes_by_scheme[
        "https"
    ].ConnectionCls()
    observer = SimpleNamespace(
        attempt=lambda endpoint: observed.append(endpoint), flush=lambda: None
    )
    with bind_attempt_observer(observer):
        with physical_attempt_scope(client, "endpoint-a"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(connection.request, "ok").result() == "ok"
                with pytest.raises(RuntimeError):
                    executor.submit(connection.request, "429").result()
        with physical_attempt_scope(client, "endpoint-b"):
            pass  # 验权或本地准入拒绝：没有走SDK连接request。
    assert physical == ["ok", "429"] and observed == ["endpoint-a", "endpoint-a"]
    assert not hasattr(client, "_sync_attempt_observer")
