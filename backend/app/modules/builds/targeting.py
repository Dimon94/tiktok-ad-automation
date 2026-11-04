"""预览定向解析是纯函数；绝不在执行时读取策略或扩大冻结国家集合。"""

from dataclasses import replace

from .scene_schemas import SceneContext
from .targeting_schemas import AudienceTargeting


def apply_targeting(
    scene: SceneContext, selected: AudienceTargeting, common: list[str]
) -> SceneContext:
    mapping = {
        item["region_code"]: item["location_id"]
        for item in scene.field_constraints.get("target_regions", ())
    }
    countries = (
        list(selected.region_codes)
        if selected.region_mode == "SELECTED"
        else sorted(set(common))
    )
    reasons = list(scene.reason_codes)
    if (
        not countries
        or not set(countries) <= set(mapping)
        or not set(countries) <= set(common)
    ):
        reasons.append("targeting_regions_unavailable")
    group = dict(scene.adgroup_fields)
    # Minis 默认的不限性别/年龄/语言定向不发送这些冗余字段。
    # 用户选择额外限制时明确发送 MANUAL，保留原始定向意图；
    # 不再用本地未核验标记提前阻断，实际支持范围由创建接口返回。
    restrictive = (
        bool(selected.languages)
        or bool(selected.age_groups)
        or selected.gender != "GENDER_UNLIMITED"
    )
    if restrictive:
        group["targeting_optimization_mode"] = "MANUAL"
    group["targeting_spec"] = {
        "location_ids": sorted(mapping[code] for code in countries if code in mapping),
        **(
            {"gender": selected.gender}
            if selected.gender != "GENDER_UNLIMITED"
            else {}
        ),
        **({"languages": list(selected.languages)} if selected.languages else {}),
        **({"age_groups": list(selected.age_groups)} if selected.age_groups else {}),
    }
    return replace(
        scene,
        supported=scene.supported and not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        adgroup_fields=group,
        field_constraints={
            **scene.field_constraints,
            "audience_targeting": selected.model_dump(mode="json"),
            "selected_region_codes": countries,
        },
    )
