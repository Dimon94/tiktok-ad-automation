"""R18: exact request marker plus complete frozen fields, never a name alone."""

from copy import deepcopy

import pytest

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.modules.builds.readback_compare import parse_page
from app.modules.builds.reconciliation_scan import advance
from app.modules.builds.request_compiler import decode_intent
from tests.modules.builds.test_native_readback import group_body, observed_group


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("unique", "COMPLETE"),
        ("ambiguous", "UNKNOWN"),
        ("wrong_parent", "UNKNOWN"),
        ("missing", "UNKNOWN"),
        ("different", "UNKNOWN"),
        ("empty", "UNKNOWN"),
        ("old_name", "UNKNOWN"),
    ],
)
def test_native_unknown_scan(mode, expected):
    body, row = group_body(), observed_group()
    name = "Series-series-v2-R0123456789abcdef0123456789abcdef-G1"
    body["adgroup_name"] = row["adgroup_name"] = (
        name if mode != "old_name" else "same-title"
    )
    if mode == "wrong_parent":
        row["campaign_id"] = "other"
    if mode == "missing":
        del row["identity_id"]
    if mode == "different":
        row["native_series_id"] = "other"
    rows = [row]
    if mode == "ambiguous":
        second = deepcopy(row)
        second["adgroup_id"] = "second"
        rows.append(second)
    if mode == "empty":
        rows = []
    query = BuildReadQuery(intent=decode_intent("ADGROUP", body), native_series=True)
    page = parse_page(
        query=query,
        response=McpBusinessResponse(
            data={
                "list": rows,
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": len(rows),
                },
            },
            evidence=CallEvidence(),
        ),
    )
    _, result = advance(
        query=query,
        progress={"page": 1, "seen": 0, "matches": 0, "stage": "OBJECT"},
        page=page,
        seen_before=lambda _: False,
    )
    assert result == expected
