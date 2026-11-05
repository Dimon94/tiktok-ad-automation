"""所有分页ID都参与去重，包括未选中的远端素材。"""

import pytest
from sqlmodel import Session, select

from app.modules.builds.scene_job_models import SceneJobPage
from tests.modules.builds.scene.support import (
    enqueue,
    ensure,
    page,
    run,
    scene_responses,
)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("cross_page", [False, True])
def test_repeated_unselected_minis_id_never_completes(
    database_engine, redis_client, scene_case, gateway_wire, cross_page
):
    receipt = ensure(database_engine, scene_case)
    # 先完成真实的身份读取；并行续读由已保存事实决定，不能只改游标冒充前序完成。
    enqueue(gateway_wire, "identity", scene_responses(scene_case)["identity"])
    assert (
        run(database_engine, redis_client, scene_case, receipt.job_id).resource
        == "minis"
    )
    if cross_page:
        enqueue(
            gateway_wire,
            "minis",
            page([{"minis_id": f"other-{n}"} for n in range(50)], total=51),
        )
        first = run(database_engine, redis_client, scene_case, receipt.job_id)
        assert first.next_page == 2 and first.status == "PENDING", first.error_code
        data = page([{"minis_id": "other-0"}], number=2, total=51)
    else:
        data = page([{"minis_id": "same"}, {"minis_id": "same"}])
    enqueue(gateway_wire, "minis", data)
    job = run(database_engine, redis_client, scene_case, receipt.job_id)
    assert job.status == "FAILED" and job.error_code in {
        "scene_response_unverified",
        "scene_pagination_changed",
    }
    with Session(database_engine) as db:
        pages = db.exec(
            select(SceneJobPage).where(
                SceneJobPage.job_id == job.id,
                SceneJobPage.resource == "minis",
            )
        ).all()
        assert len(pages) == int(cross_page)
        assert all(len(row.facts["item_id_hashes"]) <= 50 for row in pages)
