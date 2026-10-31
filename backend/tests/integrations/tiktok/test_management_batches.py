"""只替换官方SDK/MCP传输边界；不调用真实广告接口。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters import management_batches as batches
from app.integrations.tiktok.adapters.mcp_management import McpManagementOperations
from app.integrations.tiktok.adapters.sdk_management import SdkManagementOperations
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.contracts.management import (
    MAX_MATERIAL_BATCH,
    ManagementCommand,
)

TENANT = uuid4()


def command(
    identity="one",
    *,
    field="status",
    kind="campaign",
    smart=False,
    tenant=TENANT,
    account="adv",
    status="DISABLE",
    original=None,
    material=None,
):
    return ManagementCommand(
        EntityRef(tenant, account, kind, identity),
        field,
        {"ad_type": "SMART_PLUS" if smart else "REGULAR", **(original or {})},
        {"status": status}
        if field in {"status", "material_status"}
        else {"budget": "123.45"}
        if field == "budget"
        else {"campaign_name": "name"}
        if field == "name"
        else {"roas_bid": "1.20"},
        material,
    )


class Wire:
    default_headers = {"Access-Token": "synthetic"}

    def __init__(self, response=None, failure=None):
        self.response = response or {
            "code": 0,
            "data": {"campaign_ids": ["one", "two"]},
            "request_id": "req",
        }
        self.failure = failure
        self.calls = []
        self.last_response = None

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure
        self.last_response = SimpleNamespace(data=json.dumps(self.response))
        return SimpleNamespace(get=lambda: None)

    call_api = send


@pytest.fixture
def _sdk_boundary(monkeypatch):
    class Api:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            if name.endswith("budget_update"):
                raise AttributeError(name)
            return self.client.send

    for name in ("CampaignCreationApi", "AdgroupApi", "AdApi"):
        monkeypatch.setattr(batches.sdk, name, Api)


def adapter(wire, *, guard=None, deny=False):
    scopes = []

    @contextmanager
    def scope(*args):
        scopes.append(args)
        yield

    def check(*_args):
        if deny:
            raise DomainError("permission_denied", "denied")

    instance = SdkManagementOperations(
        wire,
        request_scope=scope,
        management_scope=scope,
        capability_check=check,
        before_send=guard,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    return instance, scopes


def test_41_status_members_send_20_20_1(_sdk_boundary):
    commands = tuple(command(str(i)) for i in range(41))
    planned = batches.plan_batches(commands)
    assert [len(batch) for batch in planned] == [20, 20, 1]
    wire = Wire()
    instance, scopes = adapter(wire)
    for batch in planned:
        wire.response = {
            "code": 0,
            "data": {"campaign_ids": [c.ref.remote_id for c in batch]},
        }
        assert {r.outcome for r in instance.apply_batch(batch)} == {"ACCEPTED"}
    assert len(wire.calls) == len(scopes) == 3
    assert [len(call[1]["body"]["campaign_ids"]) for call in wire.calls] == [20, 20, 1]
    assert scopes[0][-1] == "POST /open_api/v1.3/campaign/status/update/"


@pytest.mark.parametrize(
    "other",
    [
        command("two", account="other"),
        command("two", tenant=uuid4()),
        command("two", smart=True),
        command("two", status="ENABLE"),
        command("two", kind="adgroup"),
        command("two", field="budget"),
    ],
)
def test_mixed_boundaries_never_send(other, _sdk_boundary):
    wire = Wire()
    instance, scopes = adapter(wire)
    assert {r.outcome for r in instance.apply_batch((command(), other))} == {"NOT_SENT"}
    assert not scopes and not wire.calls
    assert len(batches.plan_batches((command(), other))) == 2


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
def test_status_routes_and_member_keys(smart, kind, _sdk_boundary):
    commands = tuple(command(i, smart=smart, kind=kind) for i in ("one", "two"))
    key = "smart_plus_ad_ids" if smart and kind == "ad" else f"{kind}_ids"
    wire = Wire({"code": 0, "data": {key: ["one", "two"]}})
    instance, scopes = adapter(wire)
    assert [r.outcome for r in instance.apply_batch(commands)] == [
        "ACCEPTED",
        "ACCEPTED",
    ]
    assert wire.calls[0][1]["body"][key] == ["one", "two"]
    assert (
        scopes[0][-1]
        == f"POST /open_api/v1.3/{'smart_plus/' if smart else ''}{kind}/status/update/"
    )


def test_duplicate_and_oversized_batches_are_not_sent(_sdk_boundary):
    wire = Wire()
    instance, _ = adapter(wire)
    for values in ((command(), command()), tuple(command(str(i)) for i in range(21))):
        assert {r.outcome for r in instance.apply_batch(values)} == {"NOT_SENT"}
    assert not wire.calls
    with pytest.raises(ValueError, match="duplicate"):
        batches.plan_batches((command(), command()))


def test_materials_separate_parent_and_carousel(_sdk_boundary):
    def material(identity, parent="ad", kind="VIDEO"):
        return command(
            parent,
            smart=True,
            kind="ad",
            field="material_status",
            material=identity,
            original={"material_type": kind},
        )

    commands = (
        material("m1"),
        material("m2"),
        material("m3", kind="CAROUSEL"),
        material("m4", parent="other"),
    )
    planned = batches.plan_batches(commands)
    assert [len(batch) for batch in planned] == [2, 1, 1]
    wire = Wire({"code": 0, "data": {"ad_material_ids": ["m1", "m2"]}})
    instance, _ = adapter(wire)
    assert [r.outcome for r in instance.apply_batch(planned[0])] == [
        "ACCEPTED",
        "ACCEPTED",
    ]
    assert wire.calls[0][1]["body"] == {
        "advertiser_id": "adv",
        "smart_plus_ad_id": "ad",
        "ad_material_ids": ["m1", "m2"],
        "operation_status": "DISABLE",
    }
    assert MAX_MATERIAL_BATCH == 20


def test_unknown_material_format_does_not_merge():
    commands = tuple(
        command("ad", smart=True, kind="ad", field="material_status", material=i)
        for i in ("m1", "m2")
    )
    assert [len(batch) for batch in batches.plan_batches(commands)] == [1, 1]


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, ["UNKNOWN", "UNKNOWN"]),
        ({"campaign_ids": ["one"]}, ["ACCEPTED", "UNKNOWN"]),
        ({"campaign_ids": ["one", "one", "two"]}, ["UNKNOWN", "ACCEPTED"]),
        ({"campaign_ids": ["one", "foreign"]}, ["UNKNOWN", "UNKNOWN"]),
        (
            {
                "campaign_ids": ["one"],
                "error_list": [{"campaign_id": "two", "code": 40002}],
            },
            ["ACCEPTED", "REJECTED"],
        ),
        (
            {
                "campaign_ids": ["one"],
                "error_list": [{"campaign_id": "one", "code": 40002}],
            },
            ["UNKNOWN", "UNKNOWN"],
        ),
        (
            {"error_list": [{"campaign_id": "two", "code": 40002}] * 2},
            ["UNKNOWN", "UNKNOWN"],
        ),
        ({"error_list": [{"campaign_id": "two"}]}, ["UNKNOWN", "UNKNOWN"]),
        (
            {"error_list": [{"campaign_id": "foreign", "code": 40002}]},
            ["UNKNOWN", "UNKNOWN"],
        ),
        ({"campaign_ids": "one"}, ["UNKNOWN", "UNKNOWN"]),
    ],
)
def test_strict_member_results(data, expected):
    receipts = batches.batch_receipts(
        (command(), command("two")), {"code": 0, "request_id": "shared", "data": data}
    )
    assert [r.outcome for r in receipts] == expected
    assert {r.request_id for r in receipts} == {"shared"}
    assert not any(r.retryable for r in receipts)


@pytest.mark.parametrize(
    "raw",
    [
        {"code": 0},
        {"code": True, "data": {}},
        None,
        {"code": 40002, "data": {"campaign_ids": ["one"]}},
    ],
)
def test_malformed_or_top_error_does_not_imply_member_success(raw):
    assert batches.batch_receipts((command(),), raw)[0].outcome == "UNKNOWN"


def test_smart_budget_uses_official_client_fixed_path(_sdk_boundary):
    commands = tuple(
        command(i, smart=True, kind="adgroup", field="budget") for i in ("one", "two")
    )
    wire = Wire({"code": 0, "data": {"adgroup_ids": ["one", "two"]}})
    instance, scopes = adapter(wire)
    assert [r.outcome for r in instance.apply_batch(commands)] == [
        "ACCEPTED",
        "ACCEPTED",
    ]
    args, kwargs = wire.calls[0]
    assert args[:2] == ("/open_api/v1.3/smart_plus/adgroup/budget/update/", "POST")
    assert kwargs["body"]["budget"] == [
        {"adgroup_id": i, "budget": 123.45} for i in ("one", "two")
    ]
    assert scopes[0][-1] == "POST /open_api/v1.3/smart_plus/adgroup/budget/update/"


def test_regular_total_budget_uses_official_client_fixed_path(_sdk_boundary):
    commands = tuple(
        command(
            i,
            kind="adgroup",
            field="budget",
            original={"budget_mode": "BUDGET_MODE_TOTAL"},
        )
        for i in ("one", "two")
    )
    wire = Wire({"code": 0, "data": {"adgroup_ids": ["one", "two"]}})
    instance, scopes = adapter(wire)
    receipts = instance.apply_batch(commands)
    assert [r.outcome for r in receipts] == ["ACCEPTED", "ACCEPTED"]
    args, kwargs = wire.calls[0]
    assert args[:2] == ("/open_api/v1.3/adgroup/budget/update/", "POST")
    assert kwargs["body"]["budget"] == [
        {"adgroup_id": i, "budget": 123.45} for i in ("one", "two")
    ]
    assert scopes[0][-1] == "POST /open_api/v1.3/adgroup/budget/update/"


@pytest.mark.parametrize(
    "field,kind,original",
    [
        ("budget", "adgroup", {"budget_mode": "BUDGET_MODE_DAY"}),
        ("budget", "campaign", {}),
        ("name", "campaign", {}),
        ("roas", "adgroup", {}),
    ],
)
def test_single_object_semantics_preserved(field, kind, original, _sdk_boundary):
    commands = tuple(
        command(i, field=field, kind=kind, original=original) for i in ("one", "two")
    )
    assert [len(batch) for batch in batches.plan_batches(commands)] == [1, 1]
    wire = Wire({"code": 0, "data": {}})
    instance, _ = adapter(wire)
    assert instance.apply_batch((commands[0],))[0].outcome == "ACCEPTED"
    assert "budget" not in wire.calls[0][1]["body"] or not isinstance(
        wire.calls[0][1]["body"]["budget"], list
    )


def test_permission_and_send_fence_fail_before_wire(_sdk_boundary):
    wire = Wire()
    instance, _ = adapter(wire, deny=True)
    assert instance.apply_batch((command(),))[0].outcome == "NOT_SENT"

    def fence():
        raise DomainError("management_lease_lost", "lost")

    instance, _ = adapter(wire, guard=fence)
    receipt = instance.apply_batch((command(),))[0]
    assert (receipt.outcome, receipt.reason) == ("NOT_SENT", "management_lease_lost")
    assert not wire.calls


def test_after_send_timeout_unknown_never_retry_entire_batch(_sdk_boundary):
    wire = Wire(failure=TimeoutError())
    instance, scopes = adapter(wire)
    receipts = instance.apply_batch((command(), command("two")))
    assert {r.outcome for r in receipts} == {"UNKNOWN"}
    assert not any(r.retryable for r in receipts)
    assert len(wire.calls) == len(scopes) == 1


def test_mcp_contract_bound_to_actual_tool_and_member_results():
    class McpWire:
        _contracts = {
            "management.set_status": SimpleNamespace(tool_name="campaign_status_update")
        }

        def has_contract(self, operation):
            return operation in self._contracts

        def call(self, **kwargs):
            self.arguments = kwargs
            return McpBusinessResponse(
                {
                    "campaign_ids": ["one"],
                    "error_list": [{"campaign_id": "two", "code": 40002}],
                },
                CallEvidence(request_id="shared"),
            )

    wire = McpWire()
    instance = McpManagementOperations(wire, capability_check=lambda *args: None)
    assert [r.outcome for r in instance.apply_batch((command(), command("two")))] == [
        "ACCEPTED",
        "REJECTED",
    ]
    assert wire.arguments["arguments"]["campaign_ids"] == ["one", "two"]
    assert instance.apply_batch((command(smart=True),))[0].outcome == "NOT_SENT"


@pytest.mark.parametrize("effect", ["NOT_SENT", "UNKNOWN"])
def test_mcp_transport_failure_preserves_effect(effect):
    class McpWire:
        _contracts = {
            "management.set_status": SimpleNamespace(tool_name="campaign_status_update")
        }

        def has_contract(self, operation):
            return True

        def call(self, **kwargs):
            raise RemoteCallError(
                "transport_failure", effect=effect, evidence=CallEvidence()
            )

    instance = McpManagementOperations(McpWire(), capability_check=lambda *args: None)
    assert instance.apply_batch((command(),))[0].outcome == effect


def test_interleaved_sdk_batches_have_independent_response_clients(_sdk_boundary):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    class InterleavedWire(Wire):
        def send(self, *args, **kwargs):
            result = super().send(*args, **kwargs)
            barrier.wait(timeout=5)
            return result

    first, _ = adapter(
        InterleavedWire(
            {"code": 0, "request_id": "first", "data": {"campaign_ids": ["one"]}}
        )
    )
    second, _ = adapter(
        InterleavedWire(
            {"code": 0, "request_id": "second", "data": {"campaign_ids": ["two"]}}
        )
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(first.apply_batch, (command(),)),
            pool.submit(second.apply_batch, (command("two"),)),
        ]
        receipts = [future.result()[0] for future in futures]
    assert [(receipt.outcome, receipt.request_id) for receipt in receipts] == [
        ("ACCEPTED", "first"),
        ("ACCEPTED", "second"),
    ]


def test_stale_sdk_response_is_not_reused(_sdk_boundary):
    class StaleWire(Wire):
        def send(self, *_args, **_kwargs):
            return SimpleNamespace(get=lambda: None)

    wire = StaleWire()
    wire.last_response = SimpleNamespace(
        data='{"code":0,"data":{"campaign_ids":["one"]}}'
    )
    instance, _ = adapter(wire)
    assert instance.apply_batch((command(),))[0].outcome == "UNKNOWN"


def test_mcp_unclassified_exception_after_call_boundary_is_unknown():
    class McpWire:
        _contracts = {
            "management.set_status": SimpleNamespace(tool_name="campaign_status_update")
        }

        def has_contract(self, _operation):
            return True

        def call(self, **_kwargs):
            raise TimeoutError()

    instance = McpManagementOperations(McpWire(), capability_check=lambda *_args: None)
    assert instance.apply_batch((command(),))[0].outcome == "UNKNOWN"


def test_image_type_without_carousel_evidence_never_merges_with_video():
    commands = tuple(
        command(
            "ad",
            smart=True,
            kind="ad",
            field="material_status",
            material=identity,
            original={"material_type": kind},
        )
        for identity, kind in (("video", "VIDEO"), ("image", "IMAGE"))
    )
    assert [len(batch) for batch in batches.plan_batches(commands)] == [1, 1]


def test_missing_budget_method_and_official_client_is_explicit_unsupported(
    _sdk_boundary,
):
    wire = SimpleNamespace(default_headers={"Access-Token": "synthetic"})
    instance, scopes = adapter(wire)
    receipt = instance.apply_batch(
        (command(kind="adgroup", field="budget", smart=True),)
    )[0]
    assert (receipt.outcome, receipt.reason) == (
        "NOT_SENT",
        "management_contract_unsupported",
    )
    assert not scopes


def test_smart_roas_series_sends_one_group_and_shares_receipt(_sdk_boundary):
    commands = tuple(
        command(
            i,
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        )
        for i in ("one", "two")
    )
    assert batches.plan_batches(commands) == (commands,)
    wire = Wire({"code": 0, "data": {}, "request_id": "linked"})
    instance, scopes = adapter(wire)
    receipts = instance.apply_batch(commands)
    assert [(r.outcome, r.reason, r.request_id) for r in receipts] == [
        ("ACCEPTED", "series_roas_linked", "linked")
    ] * 2
    assert len(wire.calls) == len(scopes) == 1
    assert wire.calls[0][1]["body"] == {
        "advertiser_id": "adv",
        "adgroup_id": "one",
        "roas_bid": 1.2,
    }
    assert scopes[0][-1] == "POST /open_api/v1.3/smart_plus/adgroup/update/"


def test_smart_roas_series_conflicting_targets_rejected_before_sending(_sdk_boundary):
    from dataclasses import replace

    one = command(
        "one",
        smart=True,
        kind="adgroup",
        field="roas",
        original={"campaign_id": "campaign"},
    )
    two = replace(
        command(
            "two",
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        ),
        desired={"roas_bid": "1.30"},
    )
    with pytest.raises(DomainError, match="冲突"):
        batches.plan_batches((one, two))
    wire = Wire()
    instance, scopes = adapter(wire)
    receipts = instance.apply_batch((one, two))
    assert {(r.outcome, r.reason) for r in receipts} == {
        ("NOT_SENT", "management_roas_series_conflict")
    }
    assert not wire.calls and not scopes


def test_roas_groups_keep_all_41_audit_members_without_planning_duplicate_requests():
    commands = tuple(
        command(
            str(i),
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        )
        for i in range(41)
    )
    assert batches.roas_link_groups(commands) == (commands,)
    assert batches.plan_batches(commands) == (commands,)
    assert batches.plan_batches(
        tuple(group[0] for group in batches.roas_link_groups(commands))
    ) == ((commands[0],),)


@pytest.mark.parametrize(
    "smart,campaigns",
    [(False, ("same", "same")), (True, (None, None)), (True, ("one", "two"))],
)
def test_roas_without_verified_same_smart_series_stays_single(smart, campaigns):
    commands = tuple(
        command(
            str(i),
            smart=smart,
            kind="adgroup",
            field="roas",
            original={"campaign_id": campaign},
        )
        for i, campaign in enumerate(campaigns)
    )
    assert [len(group) for group in batches.roas_link_groups(commands)] == [1, 1]
    assert [len(group) for group in batches.plan_batches(commands)] == [1, 1]


def test_roas_series_group_is_tenant_account_scoped_and_decimal_normalized():
    from dataclasses import replace

    one = command(
        "one",
        smart=True,
        kind="adgroup",
        field="roas",
        original={"campaign_id": "campaign"},
    )
    two = replace(
        command(
            "two",
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        ),
        desired={"roas_bid": "1.2000"},
    )
    other = replace(
        two,
        ref=EntityRef(TENANT, "other", "adgroup", "two"),
        desired={"roas_bid": "1.30"},
    )
    assert batches.roas_link_groups((one, two, other)) == ((one, two), (other,))
    assert batches.plan_batches((one, two, other)) == ((one, two), (other,))


def test_linked_roas_transport_failure_propagates_to_every_member(_sdk_boundary):
    commands = tuple(
        command(
            i,
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        )
        for i in ("one", "two")
    )
    wire = Wire(failure=TimeoutError())
    instance, _ = adapter(wire)
    assert {r.outcome for r in instance.apply_batch(commands)} == {"UNKNOWN"}
    assert len(wire.calls) == 1


def test_21_linked_roas_audit_members_still_send_only_one_platform_id(_sdk_boundary):
    commands = tuple(
        command(
            str(i),
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        )
        for i in range(21)
    )
    assert batches.plan_batches(commands) == (commands,)
    wire = Wire({"code": 0, "data": {}, "request_id": "linked-21"})
    instance, scopes = adapter(wire)
    receipts = instance.apply_batch(commands)
    assert len(receipts) == 21
    assert {(r.outcome, r.reason, r.request_id) for r in receipts} == {
        ("ACCEPTED", "series_roas_linked", "linked-21")
    }
    assert len(wire.calls) == len(scopes) == 1
    assert wire.calls[0][1]["body"] == {
        "advertiser_id": "adv",
        "adgroup_id": "0",
        "roas_bid": 1.2,
    }


def test_roas_mixed_campaign_audit_batch_is_not_sent(_sdk_boundary):
    commands = tuple(
        command(
            str(i),
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": str(i)},
        )
        for i in range(21)
    )
    wire = Wire()
    instance, scopes = adapter(wire)
    assert {r.outcome for r in instance.apply_batch(commands)} == {"NOT_SENT"}
    assert not wire.calls and not scopes


def test_mcp_linked_roas_uses_one_single_group_tool_call():
    class McpWire:
        calls = []

        def has_contract(self, operation):
            return operation == "management.update_roas"

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return McpBusinessResponse({}, CallEvidence(request_id="mcp-linked"))

    commands = tuple(
        command(
            str(i),
            smart=True,
            kind="adgroup",
            field="roas",
            original={"campaign_id": "campaign"},
        )
        for i in range(21)
    )
    wire = McpWire()
    instance = McpManagementOperations(wire, capability_check=lambda *_args: None)
    receipts = instance.apply_batch(commands)
    assert len(receipts) == 21
    assert {(r.outcome, r.reason, r.request_id) for r in receipts} == {
        ("ACCEPTED", "series_roas_linked", "mcp-linked")
    }
    assert len(wire.calls) == 1
    assert wire.calls[0]["arguments"] == {
        "advertiser_id": "adv",
        "adgroup_id": "0",
        "roas_bid": 1.2,
    }


def test_actual_smart_campaign_list_receipts_preserve_member_identity():
    receipts = batches.batch_receipts(
        (command("one", smart=True), command("two", smart=True)),
        {
            "code": 0,
            "request_id": "actual-response-shape",
            "data": {
                "campaign_list": [
                    {"campaign_id": "one", "status": "DISABLE"},
                    {"campaign_id": "two", "status": "DISABLE"},
                ],
                "error_list": [],
            },
        },
    )
    assert [receipt.outcome for receipt in receipts] == ["ACCEPTED", "ACCEPTED"]
    assert {receipt.request_id for receipt in receipts} == {"actual-response-shape"}


def test_campaign_list_wrong_status_never_proves_requested_pause():
    receipts = batches.batch_receipts(
        (command("one", smart=True),),
        {
            "code": 0,
            "data": {"campaign_list": [{"campaign_id": "one", "status": "ENABLE"}]},
        },
    )
    assert receipts[0].outcome == "UNKNOWN"
