"""官方 SDK VID 上传的真实传输合同；网络在 HTTP 边界替身。"""

import json

import pytest

from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.materials import VIDVideoUpload
from tests.integrations.tiktok.test_material_upload_adapters import (
    upload_case as upload_case,
)


@pytest.mark.parametrize("upload_case", ["SDK"], indirect=True)
def test_vid_upload_uses_json_and_existing_video_upload_admission(upload_case):
    _, opened, enqueue, budget, calls, _, admissions = upload_case
    enqueue([{"video_id": "receipt-vid", "material_id": "receipt-mid"}])
    with opened() as adapter:
        result = adapter.upload_video_id(
            VIDVideoUpload("123", "v1c033g50000dbk8b4nog65o4ru0ciu0", "stable.mp4"),
            budget=budget,
        )
    assert result.video_id == "receipt-vid" and result.mid == "receipt-mid"
    assert len(calls) == 1
    assert ("123", "materials.upload_video_url") in admissions
    body = json.loads(calls[0][2]["body"])
    assert body["upload_type"] == "UPLOAD_BY_VIDEO_ID"
    assert body["video_id"] == "v1c033g50000dbk8b4nog65o4ru0ciu0"
    assert body["auto_fix_enabled"] is False and body["flaw_detect"] is False
    assert "video_url" not in body and "video_signature" not in body


@pytest.mark.parametrize("upload_case", ["SDK"], indirect=True)
def test_vid_nonzero_upload_is_unknown_and_never_retried(upload_case):
    _, opened, enqueue, budget, calls, _, _ = upload_case
    enqueue({}, code=40002)
    with opened() as adapter, pytest.raises(RemoteCallError) as error:
        adapter.upload_video_id(
            VIDVideoUpload("123", "v1c033g50000dbk8b4nog65o4ru0ciu0", "stable.mp4"),
            budget=budget,
        )
    assert error.value.effect == "UNKNOWN"
    assert len(calls) == 1
