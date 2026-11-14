from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds import batch_numbers, previews
from app.modules.builds.drafts import create_draft
from app.modules.builds.preview_models import BuildPreview, PreviewDrama
from app.modules.providers.models import PromotionLink, ProviderDrama
from tests.modules.builds.test_drafts import create_intent
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.strategies.test_versions import config


def preview_row(context, intent, draft_id, revision=1):
    strategy = config()
    return BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=intent["bc_id"],
        draft_id=draft_id,
        draft_revision=revision,
        strategy_version_id=intent["strategy_version_id"],
        actor_id=context.actor_id,
        batch_short_id="",
        local_date="20260911",
        config=strategy.model_dump(mode="json"),
        budget=strategy.budget,
        target_roas=strategy.target_roas,
    )


def test_colliding_numbers_retry_without_losing_outer_transaction(
    session, context, intent, monkeypatch
):
    draft_id = create_draft(session, context=context, **intent)
    numbers = iter(["A7K2", "A7K2", "A7K3"])
    monkeypatch.setattr(batch_numbers, "random_batch_number", lambda: next(numbers))
    first = preview_row(context, intent, draft_id)
    second = preview_row(context, intent, draft_id, revision=2)
    batch_numbers.insert_preview_with_number(session, first)
    batch_numbers.insert_preview_with_number(session, second)
    assert first.batch_short_id == "A7K2"
    assert second.batch_short_id == "A7K3"
    assert session.get(BuildPreview, first.id) is first

    # 重试耗尽必须明确失败，不能退回 UUID 或接受重复号码。
    monkeypatch.setattr(batch_numbers, "random_batch_number", lambda: "A7K2")
    with pytest.raises(DomainError) as error:
        batch_numbers.insert_preview_with_number(
            session, preview_row(context, intent, draft_id, 3)
        )
    assert error.value.code == "preview_batch_number_exhausted"
    assert session.get(BuildPreview, second.id) is second
    monkeypatch.setattr(batch_numbers, "random_batch_number", lambda: "A7K4")
    third = preview_row(context, intent, draft_id, 3)
    batch_numbers.insert_preview_with_number(session, third)
    assert (
        session.exec(select(BuildPreview).where(BuildPreview.id == third.id))
        .one()
        .batch_short_id
        == "A7K4"
    )


def test_other_integrity_errors_are_not_hidden(session, context, intent, monkeypatch):
    draft_id = create_draft(session, context=context, **intent)
    monkeypatch.setattr(batch_numbers, "random_batch_number", lambda: "A7K2")
    row = preview_row(context, intent, draft_id)
    row.strategy_version_id = uuid4()
    with pytest.raises(IntegrityError):
        batch_numbers.insert_preview_with_number(session, row)


def test_concurrent_number_collisions_commit_distinct_numbers(
    isolated_strategy_database, monkeypatch
):
    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        intent = create_intent(session, context)
        drafts = [create_draft(session, context=context, **intent) for _ in range(2)]
        session.commit()
    barrier = Barrier(2)
    state = local()

    def number():
        state.calls += 1
        return "A7K2" if state.calls == 1 else f"A7K{state.index + 3}"

    monkeypatch.setattr(batch_numbers, "random_batch_number", number)

    def work(index):
        state.index, state.calls = index, 0
        with Session(engine) as session:
            row = preview_row(context, intent, drafts[index])
            barrier.wait(timeout=10)
            batch_numbers.insert_preview_with_number(session, row)
            session.commit()
            return row.batch_short_id, state.calls

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(work, range(2)))
    assert len({number for number, _ in results}) == 2
    assert sorted(calls for _, calls in results) == [1, 2]


def test_default_names_snapshot_provider_ids_and_reuse_number(
    session, context, prepared
):
    for link in session.exec(
        select(PromotionLink).where(PromotionLink.tenant_id == context.tenant_id)
    ).all():
        link.protected_base = ""
        session.add(link)
    sources = session.exec(
        select(ProviderDrama)
        .where(ProviderDrama.tenant_id == context.tenant_id)
        .order_by(ProviderDrama.title)
    ).all()
    for number, source in enumerate(sources, 32827):
        source.external_drama_id = f"6a98f85eadb6903f924e{number}"
        source.display_drama_id = str(number)
        session.add(source)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    # 单步运行到命名输入快照完成，在账户展开前更改版权方目录。
    for _ in range(200):
        previews.continue_preview(
            session, context=context, preview_id=identity, step_limit=1
        )
        row = session.get(BuildPreview, identity)
        if row.progress["phase"] == "units":
            break
    else:
        raise AssertionError("snapshot never finished")
    assert len(row.batch_short_id) == 4
    assert set(row.batch_short_id) <= set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    originals = session.exec(
        select(PreviewDrama).where(PreviewDrama.preview_id == identity)
    ).all()
    expected = {
        drama.drama_id: f"jiashu-{drama.title}-{drama.display_drama_id}-{row.batch_short_id}"
        for drama in originals
    }
    for drama in originals:
        assert drama.provider_pinyin == "jiashu"
        assert drama.display_drama_id in ("32827", "32828")
        source = session.get(ProviderDrama, drama.drama_id)
        source.external_drama_id = "changed-" + source.external_drama_id
        source.display_drama_id = "9" + source.display_drama_id
        session.add(source)
    session.flush()
    drain(session, context, identity)
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == identity
    )
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    assert len(units) == 6
    for unit in units:
        frozen = previews.load_frozen_unit(
            session, context=context, unit_id=unit.unit_id
        )
        assert frozen.campaign_name == expected[unit.drama_id]
        assert "6a98f85eadb6903f924e" not in frozen.campaign_name
        assert str(unit.drama_id) not in frozen.campaign_name


@pytest.mark.parametrize("old_number", ["123456789012", "a" * 32])
def test_old_unfinished_preview_requires_rebuild(
    session, context, prepared, old_number
):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    row = session.get(BuildPreview, identity)
    row.batch_short_id = old_number
    session.add(row)
    session.flush()
    assert previews.continue_preview(session, context=context, preview_id=identity)
    assert row.status == "FAILED" and row.error_code == "preview_naming_outdated"


def test_unfinished_preview_without_display_id_naming_marker_requires_rebuild(
    session, context, prepared
):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    row = session.get(BuildPreview, identity)
    row.progress = {
        key: value for key, value in row.progress.items() if key != "naming_drama_id"
    }
    session.add(row)
    session.flush()

    assert previews.continue_preview(session, context=context, preview_id=identity)
    assert row.status == "FAILED" and row.error_code == "preview_naming_outdated"


@pytest.mark.parametrize(
    "value,valid",
    [
        ("A7K2", True),
        ("0000", True),
        ("ZZZZ", True),
        ("1234", True),
        ("a7k2", False),
        ("Ａ7K2", False),
        ("A7K", False),
        ("A7K22", False),
        ("A7_2", False),
        ("123456789012", False),
    ],
)
def test_short_number_accepts_only_four_uppercase_ascii_characters(value, valid):
    assert batch_numbers.is_current_batch_number(value) is valid


def test_random_short_number_uses_all_digits_and_uppercase_letters(monkeypatch):
    values = iter("A7K2")

    def choose(alphabet):
        assert alphabet == "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return next(values)

    monkeypatch.setattr(batch_numbers.secrets, "choice", choose)
    assert batch_numbers.random_batch_number() == "A7K2"


@pytest.mark.parametrize("frozen", [False, True])
def test_previous_two_template_preview_cannot_mix_naming_rules(
    session, context, prepared, frozen, monkeypatch
):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    row = session.get(BuildPreview, identity)
    legacy_config = row.config | {
        "campaign_suffix": "-{YYYYMMDD}-{batch_short_id}",
        "campaign_name_template": "{provider_pinyin}-{drama_name}-{drama_id}-{random}",
    }
    if frozen:
        original_digest = previews._digest_units

        def freeze_legacy_config(db, value, scope):
            complete = original_digest(db, value, scope)
            if complete:
                # 先完整构建冻结源，再在同一冻结事务保存历史命名合同。
                value.config = legacy_config
            return complete

        monkeypatch.setattr(previews, "_digest_units", freeze_legacy_config)
        drain(session, context, identity)
    else:
        row.config = legacy_config
    session.add(row)
    session.flush()
    session.refresh(row)
    original = row.model_dump(mode="json")
    assert previews.continue_preview(session, context=context, preview_id=identity)
    session.refresh(row)
    if frozen:
        assert row.model_dump(mode="json") == original
    else:
        assert row.status == "FAILED" and row.error_code == "preview_naming_outdated"
        assert row.config == original["config"]
