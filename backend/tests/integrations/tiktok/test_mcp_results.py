"""仅使用本地合成业务 envelope；这些 fixture 不是官方已观察响应。"""

import json
import logging
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.mcp.protocol import ToolContract, load_tool_contracts
from app.integrations.tiktok.mcp.results import decode_mcp_result


def contract(*, shape="OBJECT", text=False):
    return ToolContract(
        operation="builds.create_campaign",
        tool_name="fixture_create",
        effect="WRITE",
        input_schema={"type": "object"},
        output_schema=None,
        response_shape=shape,
        source_urls=("https://example.com/local-fixture",),
        text_json_envelope=text,
    )


def receipt(raw=None, *, texts=(), is_error=False, **kwargs):
    return CallToolResult(
        structured_content=raw,
        content=[TextContent(type="text", text=value) for value in texts],
        is_error=is_error,
        **kwargs,
    )


def assert_unknown(result, *, selected_contract=None, code=None):
    with pytest.raises(RemoteCallError) as exc:
        decode_mcp_result(result, contract=selected_contract or contract())
    assert exc.value.effect == "UNKNOWN"
    assert exc.value.retryable is False
    if code:
        assert exc.value.code == code
    return exc.value


def test_success_text_without_business_receipt_is_unknown():
    assert_unknown(receipt(texts=["创建成功"]))


@pytest.mark.parametrize(
    "shape,data",
    [
        ("OBJECT", {"campaign_id": "900719925474099312345"}),
        (
            "OBJECT_LIST",
            [{"campaign_id": "900719925474099312345"}, {"id": 900719925474099312345}],
        ),
        ("OBJECT_LIST", []),
    ],
)
def test_structured_success_preserves_exact_data_and_evidence(shape, data):
    decoded = decode_mcp_result(
        receipt(
            {
                "code": 0,
                "data": data,
                "request_id": "req-123",
                "mcp_request_id": 900719925474099312345,
                "remote_task_id": "task-456",
            }
        ),
        contract=contract(shape=shape),
    )
    assert decoded.data == data
    assert decoded.evidence == CallEvidence(
        "req-123", "900719925474099312345", "task-456"
    )


def test_is_error_overrides_apparently_successful_business_receipt():
    error = assert_unknown(
        receipt({"code": 0, "data": {}, "request_id": "req-123"}, is_error=True)
    )
    assert error.evidence.request_id == "req-123"


@pytest.mark.parametrize(
    "raw",
    [
        {"code": 10001, "data": {}},
        {"code": -1, "data": {}, "message": "安全重试"},
    ],
)
def test_business_rejection_does_not_prove_no_effect(raw):
    error = assert_unknown(receipt(raw), code="mcp_business_error")
    assert error.evidence.remote_code == raw["code"]


@pytest.mark.parametrize("text", [False, True])
@pytest.mark.parametrize("remote_code", [40002, 51002])
def test_ad_known_no_effect_rejection_is_typed_for_bounded_retry(
    text, remote_code, caplog
):
    selected = next(
        c for c in load_tool_contracts() if c.operation == "build.create_ad"
    )
    message = (
        "Invalid param(s): video material should have 1 image_info as the video cover."
    )
    raw = {
        "code": remote_code,
        "message": message,
        "request_id": "synthetic-cover-request",
    }
    result = receipt(texts=[json.dumps(raw)]) if text else receipt(raw)
    with pytest.raises(RemoteCallError) as exc:
        decode_mcp_result(result, contract=selected)
    error = exc.value
    assert error.effect == "REJECTED_NO_EFFECT"
    assert error.code == "mcp_business_error"
    assert error.evidence == CallEvidence(
        request_id="synthetic-cover-request",
        remote_code=remote_code,
        remote_message=message,
    )
    for output in (str(error), repr(error), caplog.text):
        assert message not in output


def test_ad_unlisted_business_rejection_stays_unknown():
    selected = next(
        c for c in load_tool_contracts() if c.operation == "build.create_ad"
    )
    error = assert_unknown(
        receipt({"code": 40003, "message": "synthetic"}),
        selected_contract=selected,
        code="mcp_business_error",
    )
    assert error.evidence.remote_code == 40003


@pytest.mark.parametrize("value", [True, 40002.0, "40002", None])
def test_remote_code_is_never_coerced_from_invalid_business_code(value):
    error = assert_unknown(
        receipt({"code": value, "data": {}}), code="mcp_response_invalid"
    )
    assert error.evidence.remote_code is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"data": {}},
        {"code": 0},
        {"code": False, "data": {}},
        {"code": 0.0, "data": {}},
        {"code": "0", "data": {}},
        {"code": None, "data": {}},
        {"code": 0, "data": None},
        {"code": 0, "data": "成功"},
        {"code": 0, "data": []},
        {"code": 0, "data": {"amount": float("nan")}},
        {"code": 0, "data": {"amount": float("inf")}},
        {"code": 0, "data": {"object": object()}},
    ],
)
def test_malformed_structured_receipt_is_unknown(raw):
    assert_unknown(receipt(raw))


@pytest.mark.parametrize("data", [{}, [None], [1], [{}, "bad"]])
def test_object_list_contract_rejects_wrong_data(data):
    assert_unknown(
        receipt({"code": 0, "data": data}),
        selected_contract=contract(shape="OBJECT_LIST"),
    )


def test_text_json_requires_explicit_contract_permission():
    raw = {"code": 0, "data": {"id": "123"}}
    assert_unknown(receipt(texts=[json.dumps(raw)]))
    decoded = decode_mcp_result(
        receipt(texts=[json.dumps(raw)]), contract=contract(text=True)
    )
    assert decoded.data == raw["data"]


def test_text_list_receipt_preserves_large_integer_id():
    decoded = decode_mcp_result(
        receipt(texts=['{"code":0,"data":[{"id":900719925474099312345}]}']),
        contract=contract(shape="OBJECT_LIST", text=True),
    )
    assert decoded.data == [{"id": 900719925474099312345}]


@pytest.mark.parametrize(
    "text",
    [
        "创建成功",
        '```json\n{"code":0,"data":{}}\n```',
        '结果：{"code":0,"data":{}}',
        '{"code":0,"data":{}} trailing',
        '{"code":0,"data":{}} {"code":0,"data":{}}',
        '{"code":100,"code":0,"data":{}}',
        '{"code":0,"data":{"id":"1","id":"2"}}',
        '{"code":0,"data":{"amount":NaN}}',
        '{"code":0,"data":{"amount":Infinity}}',
        '{"code":0,"data":{"amount":1e999}}',
        "[]",
        "null",
    ],
)
def test_text_json_must_be_single_complete_unambiguous_receipt(text):
    assert_unknown(receipt(texts=[text]), selected_contract=contract(text=True))


def test_multiple_text_receipts_are_ambiguous_even_if_identical():
    text = '{"code":0,"data":{}}'
    assert_unknown(receipt(texts=[text, text]), selected_contract=contract(text=True))


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize(
    "other",
    [
        {"code": 100, "data": {"id": "1"}},
        {"code": 0, "data": {"id": "2"}},
        {"code": 0, "data": {"id": 1}},
        {"code": 0, "data": {"id": "1"}, "request_id": "different"},
    ],
)
def test_structured_and_text_conflicts_are_unknown(allowed, other):
    assert_unknown(
        receipt({"code": 0, "data": {"id": "1"}}, texts=[json.dumps(other)]),
        selected_contract=contract(text=allowed),
    )


def test_matching_text_and_structured_receipt_is_accepted():
    raw = {"code": 0, "data": {"id": "1"}}
    decoded = decode_mcp_result(
        receipt(raw, texts=[json.dumps(raw)]), contract=contract()
    )
    assert decoded.data == raw["data"]


def test_structured_receipt_does_not_need_natural_language_interpretation():
    decoded = decode_mcp_result(
        receipt({"code": 0, "data": {}}, texts=["创建失败，请重试"]),
        contract=contract(),
    )
    assert decoded.data == {}


def test_invalid_structured_content_never_falls_back_to_text():
    assert_unknown(
        receipt({"message": "ok"}, texts=['{"code":0,"data":{}}']),
        selected_contract=contract(text=True),
    )


def test_noncomplete_result_is_not_business_success():
    assert_unknown(receipt({"code": 0, "data": {}}, result_type="input_required"))


def test_unknown_fields_and_secrets_are_not_logged_or_carried_in_errors(caplog):
    secret = "Bearer sensitive-token-123"
    url = "https://material.example/video?signature=private"
    with caplog.at_level(logging.DEBUG):
        error = assert_unknown(
            receipt(
                {
                    "code": 100,
                    "data": {},
                    "message": secret,
                    "unknown": url,
                    "request_id": "req-123",
                    "remote_task_id": {"token": secret},
                    "mcp_request_id": url,
                },
                texts=[secret],
                meta={"secret": secret},
            )
        )
    assert error.evidence == CallEvidence(request_id="req-123", remote_code=100)
    assert not hasattr(error, "raw")
    for output in [str(error), repr(error), repr(vars(error)), caplog.text]:
        assert secret not in output
        assert url not in output


@pytest.mark.parametrize(
    "value", [None, True, False, 1.5, {}, [], "", "with spaces", "a" * 257]
)
def test_evidence_only_accepts_bounded_exact_identifier_values(value):
    decoded = decode_mcp_result(
        receipt({"code": 0, "data": {}, "request_id": value}), contract=contract()
    )
    assert decoded.evidence == CallEvidence()


def test_shared_context_is_frozen_and_forbids_unknown_fields():
    fields = {
        "tenant_id": uuid4(),
        "bc_id": "bc-1",
        "connection_id": uuid4(),
        "channel": "OFFICIAL_MCP",
        "authorization_revision": 1,
        "adapter_contract_revision": "fixture-v1",
    }
    route = FrozenTikTokRoute(**fields)
    with pytest.raises(ValidationError):
        route.bc_id = "bc-2"
    with pytest.raises(ValidationError):
        FrozenTikTokRoute(**fields, token="never-store")
    with pytest.raises(ValidationError):
        FrozenTikTokRoute(**{**fields, "channel": "CUSTOM_HTTP"})


def test_shared_evidence_and_response_are_frozen_dataclasses():
    evidence = CallEvidence(request_id="req-123")
    response = McpBusinessResponse(data={}, evidence=evidence)
    with pytest.raises(FrozenInstanceError):
        evidence.request_id = "changed"
    with pytest.raises(FrozenInstanceError):
        response.evidence = CallEvidence()


def test_error_text_receipt_keeps_safe_correlation_evidence():
    error = assert_unknown(
        receipt(texts=['{"code":100,"data":{},"request_id":"req-123"}'], is_error=True),
        selected_contract=contract(text=True),
    )
    assert error.evidence == CallEvidence(request_id="req-123")


def test_malformed_json_has_no_raw_exception_context():
    error = assert_unknown(
        receipt(texts=['{"secret":"private-token", "code":0,']),
        selected_contract=contract(text=True),
    )
    assert error.__cause__ is None
    assert error.__context__ is None


def test_cyclic_structured_data_is_unknown():
    data = {}
    data["self"] = data
    assert_unknown(receipt({"code": 0, "data": data}))


@pytest.mark.parametrize("operation", ["accounts.list_bcs", "accounts.get_advertisers"])
def test_documented_account_tools_accept_single_text_json_receipt(operation):
    from app.integrations.tiktok.mcp.protocol import load_tool_contracts

    selected = next(c for c in load_tool_contracts() if c.operation == operation)
    payload = {"code": 0, "data": {"list": []}, "request_id": "synthetic-read-receipt"}
    result = decode_mcp_result(receipt(texts=[json.dumps(payload)]), contract=selected)
    assert result.data == {"list": []}
    assert result.evidence.request_id == "synthetic-read-receipt"
    assert_unknown(receipt(texts=["成功"]), selected_contract=selected)
    assert_unknown(
        receipt(texts=[json.dumps({**payload, "code": 40000})]),
        selected_contract=selected,
    )


@pytest.mark.parametrize(
    "operation",
    [c.operation for c in load_tool_contracts()],
)
def test_registered_native_text_json_is_decoded_with_request_evidence(operation):
    from app.integrations.tiktok.mcp.protocol import load_tool_contracts

    selected = next(c for c in load_tool_contracts() if c.operation == operation)
    payload = {"code": 0, "data": {"list": []}, "request_id": "actual-shaped-request"}
    result = decode_mcp_result(receipt(texts=[json.dumps(payload)]), contract=selected)
    assert result.evidence.request_id == "actual-shaped-request"
    assert result.data == payload["data"]
    assert_unknown(receipt(texts=["uploaded successfully"]), selected_contract=selected)
    assert_unknown(
        receipt(texts=[json.dumps(payload), json.dumps(payload)]),
        selected_contract=selected,
    )
    assert_unknown(
        receipt(texts=[json.dumps({**payload, "code": 40001})]),
        selected_contract=selected,
    )


def test_video_upload_singleton_array_is_not_a_multiple_file_receipt():
    from app.integrations.tiktok.mcp.protocol import load_tool_contracts
    from app.modules.materials.sdk_assets import video_upload_receipt

    selected = next(
        c for c in load_tool_contracts() if c.operation == "materials.upload_video_url"
    )
    row = {"video_id": "actual-vid", "material_id": "actual-mid"}
    payload = {"code": 0, "data": [row], "request_id": "actual-shaped-request"}
    result = decode_mcp_result(receipt(texts=[json.dumps(payload)]), contract=selected)
    assert (
        video_upload_receipt(
            result, advertiser_id="123", channel="OFFICIAL_MCP"
        ).video_id
        == "actual-vid"
    )
    assert_unknown(
        receipt(texts=[json.dumps({**payload, "data": [row, row]})]),
        selected_contract=selected,
    )


@pytest.mark.parametrize(
    "operation,field",
    [("build.get_campaigns", "budget"), ("build.get_adgroups", "roas_bid")],
)
def test_native_build_read_retains_original_decimal_lexeme(operation, field):
    selected = next(c for c in load_tool_contracts() if c.operation == operation)
    text = (
        '{"code":0,"data":{"list":[{"'
        + field
        + '":1.234567890123456789,"other":1.25}]}}'
    )
    result = decode_mcp_result(receipt(texts=[text]), contract=selected)
    assert result.data["list"][0][field] == "1.234567890123456789"
    assert result.data["list"][0]["other"] == 1.25
    structured = decode_mcp_result(receipt(raw=json.loads(text)), contract=selected)
    assert isinstance(structured.data["list"][0][field], float)


@pytest.mark.parametrize(
    "structured_code,text_code", [(40001, 0), (40001, 40002), (0, 40001)]
)
def test_contradictory_carriers_precede_business_error_classification(
    structured_code, text_code
):
    raw = {"code": structured_code, "data": {}, "request_id": "structured-receipt"}
    text = {"code": text_code, "data": {}, "request_id": "text-receipt"}
    failure = assert_unknown(
        receipt(raw, texts=[json.dumps(text)]), code="mcp_response_ambiguous"
    )
    assert failure.evidence.remote_code is None
