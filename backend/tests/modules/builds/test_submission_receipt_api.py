from uuid import uuid4

from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen
from tests.modules.strategies.test_api import headers


def test_submit_response_recovered_by_same_request_without_another_write(
    client, context, other_context, frozen
):
    request_id = uuid4()
    base = f"/api/tenants/{context.tenant_id}"
    route = f"{base}/submission-requests/{request_id}"
    assert client.get(route, headers=headers(context)).status_code == 404
    first = client.post(
        f"{base}/build-previews/{frozen}/submit",
        json={"request_id": str(request_id)},
        headers=headers(context),
    )
    assert first.status_code == 202
    restored = client.get(route, headers=headers(context))
    assert restored.status_code == 200 and restored.json() == first.json()
    replay = client.post(
        f"{base}/build-previews/{frozen}/submit",
        json={"request_id": str(request_id)},
        headers=headers(context),
    )
    assert replay.json() == first.json()
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/submission-requests/{request_id}",
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_revised_preview_waits_for_unexpanded_original_submission(
    client, session, context, frozen
):
    from uuid import UUID

    from sqlalchemy import func
    from sqlmodel import select

    from app.modules.builds import previews
    from app.modules.builds.execution_models import ExecutionStep, Submission
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.preview_models import BuildPreview
    from tests.modules.builds.test_previews import drain

    base = f"/api/tenants/{context.tenant_id}/build-previews"
    first = client.post(
        f"{base}/{frozen}/submit",
        json={"request_id": str(uuid4())},
        headers=headers(context),
    )
    assert first.status_code == 202
    original = session.get(Submission, UUID(first.json()["submission_id"]))
    draft = session.get(BuildDraft, session.get(BuildPreview, frozen).draft_id)
    draft.revision += 1
    session.flush()
    revised = previews.generate_preview(
        session, context=context, draft_id=draft.id, expected_revision=draft.revision
    )
    drain(session, context, revised)

    rejected = client.post(
        f"{base}/{revised}/submit",
        json={"request_id": str(uuid4())},
        headers=headers(context),
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "continuation_pending"
    assert "原任务" in rejected.json()["message"]
    assert "刷新续建预览" in rejected.json()["message"]
    # 解释原因不能放开重复创建，也不能改动旧任务。
    assert session.exec(select(func.count()).select_from(Submission)).one() == 1
    assert session.get(Submission, original.id).status == "QUEUED"
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == 0
