"""JB-7 approved workspace interface; real PostgreSQL persistence."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import DomainError
from app.modules.builds import drafts, mutations
from app.modules.builds.catalog import draft_summary
from app.modules.builds.schemas import CreateDraftRequest


def native_selection():
    return {
        "kind": "NATIVE_SERIES_DIRECT_VIDEO",
        "advertiser_id": "account-A",
        "native_series_id": "series-exact-1",
        "video_sources": [],
        "series_identity": {"identity_type": "TT_USER", "identity_id": "series-owner"},
        "creative_identity": {
            "identity_type": "BC_AUTH_TT",
            "identity_id": "creative-owner",
            "identity_authorized_bc_id": "bc-draft",
        },
        "regions": ["US"],
    }


def test_workspace_native_atomic_save_replay_and_revision(session, context, intent):
    values = intent | {
        "provider_connection_id": None,
        "application_id": None,
        "drama_lines": [],
        "account_lines": ["account-A"],
        "link_config": {},
        "scene_selection": native_selection(),
    }
    request = CreateDraftRequest(request_id=uuid4(), **values)
    identity = drafts.create_draft(session, context=context, **request.model_dump())
    session.flush()
    saved = draft_summary(session, context=context, draft_id=identity)
    assert saved.scene_selection.model_dump(exclude_none=True) == native_selection()
    assert saved.provider_connection_id is None and saved.application_id is None
    assert (
        drafts.create_draft(session, context=context, **request.model_dump())
        == identity
    )
    replacement = native_selection() | {
        "native_series_id": "series-exact-2",
        "regions": ["CA"],
        "creative_identity": {
            "identity_type": "TT_USER",
            "identity_id": "second-creative",
        },
    }
    args = {
        "context": context,
        "draft_id": identity,
        "request_id": uuid4(),
        "expected_revision": 1,
        "scene_selection": replacement,
    }
    assert mutations.update_draft(session, **args) == 2
    assert mutations.update_draft(session, **args) == 2
    updated = draft_summary(session, context=context, draft_id=identity)
    assert updated.scene_selection.native_series_id == "series-exact-2"
    assert updated.scene_selection.creative_identity.identity_id == "second-creative"
    assert updated.scene_selection.series_identity.identity_id == "series-owner"
    with pytest.raises(DomainError) as error:
        mutations.update_draft(
            session,
            context=context,
            draft_id=identity,
            expected_revision=1,
            scene_selection=native_selection(),
        )
    assert error.value.code == "draft_revision_conflict"
    assert draft_summary(session, context=context, draft_id=identity).revision == 2


def test_workspace_rejects_mixed_destination_without_save(intent):
    with pytest.raises(ValidationError):
        CreateDraftRequest(
            request_id=uuid4(), **(intent | {"scene_selection": native_selection()})
        )
    with pytest.raises(ValidationError):
        CreateDraftRequest(
            request_id=uuid4(),
            **(
                intent
                | {
                    "provider_connection_id": None,
                    "application_id": None,
                    "drama_lines": [],
                    "account_lines": ["account-A"],
                    "scene_selection": native_selection() | {"minis_id": "wrong"},
                }
            ),
        )
