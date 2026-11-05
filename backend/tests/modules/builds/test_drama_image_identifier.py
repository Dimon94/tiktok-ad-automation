from uuid import uuid4

from sqlmodel import select

from app.modules.builds.image_candidate_service import scoped_snapshot
from app.modules.builds.image_composition_service import refresh_candidates
from app.modules.builds.models import DraftInput
from tests.modules.builds.test_image_composition_service import (
    finish_candidate_snapshot,
)
from tests.modules.builds.test_image_composition_service import (
    image_draft as image_draft,
)
from tests.modules.materials.test_tenant_materials import material


def test_image_candidate_snapshot_freezes_identifier_instead_of_title(
    session, image_draft
):
    draft, drama, scope = image_draft
    row = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft.id,
            DraftInput.kind == "drama",
            DraftInput.line_no == drama.first_line,
        )
    ).one()
    row.manual_link = {"line_no": row.line_no, "material_identifier": "IMG_001%"}
    marker = material(session, scope, "prefix_img_001%_image.jpg")
    marker.media_type, marker.mime_type, marker.sha256 = "IMAGE", "image/jpeg", "f" * 64
    session.flush()
    progress = refresh_candidates(session, scope, uuid4(), draft.revision)
    snapshot = scoped_snapshot(session, scope, progress["snapshot_id"])
    assert snapshot.dramas[0]["title"] == "IMG_001%"
    finished = finish_candidate_snapshot(session, scope, progress)
    assert finished["candidate_count"] == 1
