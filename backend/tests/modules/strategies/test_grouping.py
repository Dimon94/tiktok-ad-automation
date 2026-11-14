from types import SimpleNamespace
from uuid import UUID, uuid4

from app.modules.strategies.copy_pool import seed_copies
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.structure import plan_structure


def test_structure_replay_is_stable_for_sorted_materials_and_copies():
    config = StrategyConfig.model_validate(
        {
            "budget": "100.00",
            "currency": "USD",
            "copy_pool_version": uuid4(),
            "group_generation_mode": "BY_MATERIAL",
            "group_count": None,
            "group_material_allocation": None,
            "max_materials_per_group": 10,
            "ad_generation_mode": "FIXED",
            "ads_per_group": 1,
            "ad_material_allocation": "SHARED",
            "max_materials_per_ad": None,
            "creative_count": 3,
        }
    )
    files = [SimpleNamespace(unit_id=UUID(int=i + 1)) for i in reversed(range(23))]

    first = plan_structure(files, config=config, pool=seed_copies(), seed=51)
    second = plan_structure(files, config=config, pool=seed_copies(), seed=51)

    assert first == second
    assert [len(group.unit_ids) for group in first] == [10, 10, 3]
    assert [len(group.ads) for group in first] == [1, 1, 1]
    assert all(len(group.ads[0].copies) == 3 for group in first)
