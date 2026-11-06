"""真实 SDK/适配器消费本地 HTTP 回执；错误码不是无副作用或可重发证明。"""

import json
import threading
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
import urllib3

from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.materials import (
    AssetShare,
    RemoteCallBudget,
    URLImageUpload,
    URLVideoUpload,
)
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.request_compiler import decode_intent
from tests.contracts.test_tiktok_build_contract import build_bodies

PRIVATE_MESSAGE = "Bearer private-token https://cdn.example/file?signature=private"
ADVERTISER = "90071992547409939999"
SUCCESS_DATA = {
    "video": [{"video_id": "video-1", "material_id": "material-1"}],
    "image": {"image_id": "image-1"},
    "share": {"failed_infos": {}},
    "ad": {"smart_plus_ad_id": "ad-1"},
}


@pytest.fixture(params=["video", "image", "share", "ad"])
def sdk_write(request, monkeypatch):
    replies, calls = deque(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            body = json.dumps(replies.popleft()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = urllib3.PoolManager.request

    def redirect(pool, method, url, **kwargs):
        assert url.startswith("https://business-api.tiktok.com/")
        local = f"http://127.0.0.1:{server.server_port}" + url.removeprefix(
            "https://business-api.tiktok.com"
        )
        return original(pool, method, local, **kwargs)

    monkeypatch.setattr(urllib3.PoolManager, "request", redirect)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    budget = RemoteCallBudget(deadline, 60, 75000)

    @contextmanager
    def scope(advertiser, _operation, actual_deadline):
        assert advertiser == ADVERTISER and actual_deadline == deadline
        yield

    def authorize(advertiser):
        assert advertiser in {ADVERTISER, "target-advertiser"}

    try:
        with official_client(access_token="synthetic-token") as client:
            materials = SDKMaterialOperations(
                client,
                request_scope=scope,
                deadline=deadline,
                share_authorize=authorize,
                api_scope_ids=frozenset({6}),
            )
            builds = ApiBuildOperations(client, request_scope=scope, deadline=deadline)

            def invoke():
                if request.param == "video":
                    return materials.upload_video_url(
                        URLVideoUpload(
                            ADVERTISER,
                            "https://cdn.example/video.mp4",
                            "video.mp4",
                            "a" * 32,
                            120,
                        ),
                        budget=budget,
                    )
                if request.param == "image":
                    return materials.upload_image_url(
                        URLImageUpload(
                            ADVERTISER, "https://cdn.example/image.jpg", "image.jpg"
                        ),
                        budget=budget,
                    )
                if request.param == "share":
                    return materials.share_assets(
                        AssetShare(ADVERTISER, ("material-1",), ("target-advertiser",)),
                        budget=budget,
                    )
                return builds.create(
                    attempt_id=uuid4(), intent=decode_intent("AD", build_bodies()["AD"])
                )

            yield request.param, invoke, replies, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("code", [40002, 20001, 50002])
def test_sdk_write_preserves_error_code_without_replay_or_private_message(
    sdk_write, code, caplog
):
    operation, invoke, replies, calls = sdk_write
    replies.append(
        {"code": code, "request_id": "provider-request", "message": PRIVATE_MESSAGE}
    )
    with pytest.raises(RemoteCallError) as caught:
        invoke()
    error = caught.value
    expected_effect = (
        "REJECTED_NO_EFFECT"
        if operation == "ad" and code in {40002, 51002}
        else "UNKNOWN"
    )
    assert error.effect == expected_effect and error.retryable is False
    assert error.evidence == CallEvidence(
        request_id="provider-request", remote_code=code
    )
    assert len(calls) == 1
    for rendered in (str(error), repr(error), repr(vars(error)), caplog.text):
        assert PRIVATE_MESSAGE not in rendered
        assert "private-token" not in rendered
        assert "signature=private" not in rendered


@pytest.mark.parametrize("code", [True, False, "40002", 40002.0, "0", 0.0, None])
def test_sdk_write_never_coerces_invalid_code_into_evidence(sdk_write, code):
    operation, invoke, replies, calls = sdk_write
    replies.append(
        {
            "code": code,
            "request_id": "provider-request",
            "data": SUCCESS_DATA[operation],
        }
    )
    with pytest.raises(RemoteCallError) as caught:
        invoke()
    assert caught.value.effect == "UNKNOWN"
    assert caught.value.evidence == CallEvidence(request_id="provider-request")
    assert len(calls) == 1


def test_sdk_write_success_does_not_record_zero_as_an_error(sdk_write):
    operation, invoke, replies, calls = sdk_write
    replies.append(
        {
            "code": 0,
            "request_id": "provider-request",
            "data": SUCCESS_DATA[operation],
        }
    )
    assert invoke().evidence == CallEvidence(request_id="provider-request")
    assert len(calls) == 1


def test_sdk_adgroup_40002_is_a_bounded_no_effect_rejection(sdk_write):
    _, invoke, replies, calls = sdk_write
    # The fixture's build operation is an AD create.  The dedicated ADGROUP
    # assertion below exercises the shared envelope directly so the SDK path
    # cannot silently turn a known business rejection into readback UNKNOWN.
    from app.integrations.tiktok.adapters.build_results import sdk_creation_envelope

    with pytest.raises(RemoteCallError) as caught:
        sdk_creation_envelope(
            {
                "code": 40002,
                "request_id": "provider-request",
                "data": {},
            },
            kind="ADGROUP",
        )
    error = caught.value
    assert error.effect == "REJECTED_NO_EFFECT"
    assert error.evidence == CallEvidence(
        request_id="provider-request", remote_code=40002
    )
    assert not calls and not replies


@pytest.mark.parametrize("sdk_write", ["ad"], indirect=True)
@pytest.mark.parametrize("code", [40002, 50002])
def test_sdk_build_preserves_actionable_platform_message(sdk_write, code):
    _, invoke, replies, calls = sdk_write
    message = "Invalid value for field targeting_optimization_mode: MANUAL"
    replies.append({"code": code, "request_id": "platform-request", "message": message})
    with pytest.raises(RemoteCallError) as caught:
        invoke()
    assert getattr(caught.value.evidence, "remote_message", None) == message
    assert caught.value.evidence.remote_code == code
    assert caught.value.effect == ("REJECTED_NO_EFFECT" if code == 40002 else "UNKNOWN")
    assert len(calls) == 1


@pytest.mark.parametrize("sdk_write", ["ad"], indirect=True)
def test_sdk_build_message_keeps_field_reason_but_redacts_credentials(
    sdk_write, caplog
):
    _, invoke, replies, calls = sdk_write
    replies.append(
        {
            "code": 40002,
            "request_id": "platform-request",
            "message": "Invalid targeting_spec.languages. "
            "Bearer private-token https://cdn.example/file?signature=private "
            'access_token="another-private-token"',
        }
    )
    with pytest.raises(RemoteCallError) as caught:
        invoke()
    message = getattr(caught.value.evidence, "remote_message", None)
    assert message and "Invalid targeting_spec.languages." in message
    for rendered in (message, repr(vars(caught.value)), caplog.text):
        assert "private-token" not in rendered
        assert "signature=private" not in rendered
        assert "https://cdn.example" not in rendered
    assert len(calls) == 1
