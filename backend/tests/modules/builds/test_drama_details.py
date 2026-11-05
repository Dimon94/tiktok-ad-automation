from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.catalog import inputs_page
from app.modules.builds.drafts import create_draft, prepare_draft
from app.modules.builds.manual_links import validate_manual_links
from app.modules.builds.models import (
    BuildDraft,
    DraftDrama,
    DraftInput,
    DraftPreparation,
)
from app.modules.providers.models import LinkPreparationItem
from tests.modules.builds.test_drafts import account
from tests.modules.builds.test_manual_links import URL, manual


def test_identifier_without_link_survives_save_and_automatic_preparation(
    session, context, intent
):
    account(session, context)
    intent.update(
        drama_lines=["自动剧"],
        manual_links=[
            {
                "line_no": 1,
                "material_identifier": "  AbC_001%  ",
            }
        ],
    )
    draft_id = create_draft(session, context=context, **intent)
    row = inputs_page(session, context=context, draft_id=draft_id, kind="drama").items[
        0
    ]
    assert row.manual_link["material_identifier"] == "AbC_001%"
    preparation = session.get(
        DraftPreparation,
        prepare_draft(
            session,
            context=context,
            draft_id=draft_id,
            request_id=uuid4(),
        ),
    )
    items = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == preparation.provider_task_id,
        )
    ).all()
    assert [(item.line_no, item.raw_input) for item in items] == [(1, "自动剧")]
    from app.modules.builds.drafts import _links_page
    from tests.modules.builds.test_drafts import ready_links

    ready_links(session, context, preparation.id, intent)
    _links_page(session, context, session.get(BuildDraft, draft_id), preparation)
    prepared = inputs_page(
        session, context=context, draft_id=draft_id, kind="drama"
    ).items[0]
    assert prepared.preparation.drama is not None
    assert prepared.preparation.url
    assert prepared.manual_link["material_identifier"] == "AbC_001%"


def test_identifier_is_literal_matching_basis_and_empty_uses_title(
    session, context, intent
):
    from app.modules.builds.drama_details import material_match_text

    account(session, context)
    intent.update(
        drama_lines=["剧名"], manual_links=[manual(material_identifier=" A_01% ")]
    )
    draft_id = create_draft(session, context=context, **intent)
    prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4())
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).one()
    draft = session.get(BuildDraft, draft_id)
    assert material_match_text(session, draft, drama) == "A_01%"
    row = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "drama",
        )
    ).one()
    row.manual_link = {**row.manual_link, "material_identifier": ""}
    session.flush()
    assert material_match_text(session, draft, drama) == "剧名"


def test_attribution_required_only_for_wangyan_manual_url():
    details = validate_manual_links(
        [{"line_no": 1, "material_identifier": "x"}], ["剧"], "wangyan"
    )
    assert details[1]["url"] == ""
    with pytest.raises(DomainError) as error:
        validate_manual_links([{"line_no": 1, "url": URL}], ["剧"], "wangyan")
    assert error.value.code == "manual_attribution_required"


@pytest.mark.parametrize("identifier", ["x" * 1001, "a\nb", "a\tb"])
def test_invalid_identifier_is_rejected(identifier):
    with pytest.raises(DomainError):
        validate_manual_links(
            [{"line_no": 1, "material_identifier": identifier}], ["剧"], "jiashu"
        )


def test_video_matching_uses_identifier_and_does_not_fallback(session, context, intent):
    from app.modules.builds.drafts import _materials_page
    from tests.modules.materials.test_tenant_materials import material

    account(session, context)
    intent.update(
        drama_lines=["剧名"], manual_links=[manual(material_identifier="M_01%")]
    )
    draft_id = create_draft(session, context=context, **intent)
    prep = session.get(
        DraftPreparation,
        prepare_draft(session, context=context, draft_id=draft_id, request_id=uuid4()),
    )
    draft = session.get(BuildDraft, draft_id)
    material(session, context, "剧名_001.mp4")
    material(session, context, "MZ01-anything.mp4")
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).one()
    _materials_page(session, context, draft, prep)
    assert drama.matched_count == 0
    matching = material(session, context, "prefix_m_01%_clip.mp4")
    drama.material_state = "pending"
    session.flush()
    _materials_page(session, context, draft, prep)
    assert drama.matched_count == 1
    from app.modules.builds.image_composition_models import DraftCreativeUnitItem

    assert session.exec(
        select(DraftCreativeUnitItem.material_id).where(
            DraftCreativeUnitItem.draft_id == draft_id
        )
    ).all() == [matching.id]


def test_supplementing_link_preserves_existing_identifier(session, context, intent):
    from app.modules.builds.manual_links import save_manual_link

    intent.update(
        drama_lines=["剧"], manual_links=[{"line_no": 1, "material_identifier": "M01"}]
    )
    draft_id = create_draft(session, context=context, **intent)
    row = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id, DraftInput.kind == "drama"
        )
    ).one()
    save_manual_link(
        session,
        context=context,
        draft_id=draft_id,
        input_id=row.id,
        expected_revision=1,
        request_id=uuid4(),
        link=manual(),
    )
    assert row.manual_link["material_identifier"] == "M01"
    assert row.manual_link["url"] == URL
