"""0051→0052 保存既有完整快照；新构建状态/游标的真实 PostgreSQL 合同。"""

from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table
from sqlmodel import Session, select

from app.modules.builds.image_composition_state import (
    ImageCandidateItem,
    ImageCandidateSnapshot,
    ImageCompositionState,
)
from tests.migration_database import historical_database
from tests.modules.builds.test_drafts import create_intent
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import material


def test_candidate_build_upgrade_preserves_completed_snapshot_and_count(monkeypatch):
    from tests.modules.builds.test_image_composition_migration import (
        historical_scaffold,
    )

    with historical_database(monkeypatch, "0051_image_state_scope") as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            intent = create_intent(session, context, historical=True)
            intent["drama_lines"] = ["Moon"]
            draft, drama = historical_scaffold(session, context, intent)
            image = material(session, context, "Moon.jpg")
            image.media_type, image.sha256 = "IMAGE", "a" * 64
            session.flush()
            identity, cutoff = uuid4(), datetime.now(UTC)
            table = Table(
                "image_candidate_snapshot",
                MetaData(),
                autoload_with=session.connection(),
            )
            session.execute(
                table.insert().values(
                    id=identity,
                    tenant_id=context.tenant_id,
                    draft_id=draft.id,
                    bc_id=draft.bc_id,
                    base_revision=draft.revision,
                    created_at=cutoff,
                )
            )
            candidate = {
                "material_id": str(image.id),
                "file_name_snapshot": "Original frozen name",
                "content_digest": image.sha256,
            }
            session.add(
                ImageCandidateItem(
                    tenant_id=context.tenant_id,
                    snapshot_id=identity,
                    drama_id=drama.drama_id,
                    material_id=image.id,
                    draft_id=draft.id,
                    bc_id=draft.bc_id,
                    candidate=candidate,
                )
            )
            session.add(
                ImageCompositionState(
                    tenant_id=context.tenant_id,
                    draft_id=draft.id,
                    bc_id=draft.bc_id,
                    snapshot_id=identity,
                )
            )
            session.commit()
            tenant_id, draft_id = context.tenant_id, draft.id
        command.upgrade(config, "0052_image_candidate_build")
        with Session(engine) as session:
            snapshot = session.get(ImageCandidateSnapshot, identity)
            assert snapshot.status == "READY" and snapshot.candidate_count == 1
            assert (
                snapshot.created_at == cutoff
                and snapshot.generation == 0
                and snapshot.actor_id is None
            )
            assert (
                session.get(ImageCompositionState, (tenant_id, draft_id)).snapshot_id
                == identity
            )
            assert (
                session.exec(
                    select(ImageCandidateItem).where(
                        ImageCandidateItem.snapshot_id == identity
                    )
                )
                .one()
                .candidate
                == candidate
            )
        # 仅一次性测试库验证命名约束的回退；没有回退当前应用数据库。
        command.downgrade(config, "0051_image_state_scope")
        command.upgrade(config, "0052_image_candidate_build")
        with Session(engine) as session:
            assert session.get(ImageCandidateSnapshot, identity).candidate_count == 1


def test_candidate_construction_metadata_matches_migrated_database(session):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    def include(obj, _name, kind, _reflected, _compare_to):
        return (
            obj.name if kind == "table" else obj.table.name
        ) == "image_candidate_snapshot"

    context = MigrationContext.configure(
        session.connection(), opts={"compare_type": True, "include_object": include}
    )
    assert compare_metadata(context, SQLModel.metadata) == []
