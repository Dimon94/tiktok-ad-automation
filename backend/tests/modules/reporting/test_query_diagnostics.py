import asyncio
import importlib.util
import json
import logging

import pytest


@pytest.fixture(autouse=True)
def enable_trace_logger(monkeypatch):
    # Alembic 的 fileConfig 会关闭此前导入的logger；生产查询不运行迁移配置。
    monkeypatch.setattr(logging.getLogger("app.query_trace"), "disabled", False)


def _trace_type():
    # 用显式断言记录尚未实施的观测合同，避免把导入错误误当行为失败。
    assert (
        importlib.util.find_spec("app.modules.reporting.query_diagnostics") is not None
    )
    from app.modules.reporting import query_diagnostics

    assert hasattr(query_diagnostics, "QueryTrace")
    return query_diagnostics.QueryTrace


def test_query_trace_redacts_scope_and_reports_cancel(caplog):
    trace_type = _trace_type()
    caplog.set_level(logging.INFO, logger="app.query_trace")
    secret = "tenant-sensitive-account-token-report-body"
    with pytest.raises(asyncio.CancelledError):
        with trace_type(scope={"tenant": secret}, dimension="material") as trace:
            trace.count("account_count", 2)
            trace.count(secret, 1)
            with trace.stage("hydrate"):
                with trace.stage(secret):
                    raise asyncio.CancelledError(secret)
    assert secret not in caplog.text
    payload = json.loads(caplog.records[-1].message)
    assert payload["outcome"] == "cancelled"
    assert payload["counts"] == {"account_count": 2}
    assert payload["timing_semantics"] == "inclusive"
    assert len(payload["fingerprint"]) == 64
    assert payload["stages"][0]["name"] == "hydrate"
    assert payload["stages"][0]["outcome"] == "cancelled"


def test_query_trace_nested_stages_are_explicitly_inclusive(caplog):
    trace_type = _trace_type()
    caplog.set_level(logging.INFO, logger="app.query_trace")
    ticks = iter([0.0, 1.0, 2.0, 4.0, 5.0, 6.0])
    with trace_type(
        scope={"shape": "one"}, dimension="ad", clock=lambda: next(ticks)
    ) as trace:
        with trace.stage("snapshot"):
            with trace.stage("aggregate"):
                trace.count("matched_rows", 50)
    payload = json.loads(caplog.records[-1].message)
    assert payload["outcome"] == "success"
    assert payload["duration_ms"] == 6000
    child, parent = payload["stages"]
    assert (child["name"], child["parent"], child["duration_ms"]) == (
        "aggregate",
        "snapshot",
        2000,
    )
    assert (parent["name"], parent["parent"], parent["duration_ms"]) == (
        "snapshot",
        None,
        4000,
    )


def test_query_trace_fingerprint_is_stable_and_error_body_is_not_logged(caplog):
    trace_type = _trace_type()
    caplog.set_level(logging.INFO, logger="app.query_trace")
    with trace_type(scope={"b": 2, "a": 1}, dimension="ad"):
        pass
    with pytest.raises(ValueError):
        with trace_type(scope={"a": 1, "b": 2}, dimension="ad"):
            raise ValueError("credential-in-exception")
    first, second = [json.loads(record.message) for record in caplog.records]
    assert first["fingerprint"] == second["fingerprint"]
    assert second["outcome"] == "error"
    assert "credential-in-exception" not in caplog.text


def test_query_trace_rejects_invalid_count_values_without_logging_payload(caplog):
    trace_type = _trace_type()
    caplog.set_level(logging.INFO, logger="app.query_trace")
    with trace_type(scope={}, dimension="sensitive-dimension") as trace:
        for value in [True, -1, "secret", 1.5]:
            trace.count("matched_rows", value)
    payload = json.loads(caplog.records[-1].message)
    assert payload["dimension"] is None
    assert payload["counts"] == {}
