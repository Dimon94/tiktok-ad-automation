"""版权方取链参数契约：字段、来源、校验与合并的唯一实现。

字段只保留版权方后台建链接口实际允许人工选择的参数；内部参数（小程序 ID、
平台、链路类型、剧集内部 ID、别名）由取链流程按后台网页/CLI 规则自动填写。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError

FieldSource = Literal["application_default", "per_drama"]


class LinkConfigOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)


class LinkConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=255)
    type: Literal["integer", "select"]
    required: bool = False
    default: Any = None
    options: list[LinkConfigOption] = Field(default_factory=list)
    # application_default：可在推广应用保存默认值；per_drama：未调整时逐剧自动取值。
    source: FieldSource
    rule: str | None = None
    minimum: int = 1


class LinkConfigSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_kind: str = Field(min_length=1, max_length=32)
    fields: list[LinkConfigField] = Field(max_length=50)
    defaults: dict[str, Any] = Field(default_factory=dict)
    application_defaults: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(gt=0, strict=True)


def _invalid(message: str = "取链参数无效", code: str = "link_config_invalid") -> DomainError:
    return DomainError(code, message)


def payment_template_required(application: Any) -> bool:
    """刚刚好付费、混合变现的应用建链必须带支付模板，与后台和 CLI 一致。"""

    config = getattr(application, "channel_config", None) or {}
    return str(config.get("delivery_mode", "iaa")).lower() in {"iap", "mixed"}


def _template_options(application: Any) -> list[LinkConfigOption]:
    config = getattr(application, "channel_config", None) or {}
    rows = config.get("payment_templates") or []
    return [
        LinkConfigOption(value=str(row["id"]), label=str(row["name"]))
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None and row.get("name")
    ]


def _fields(kind: str, application: Any) -> list[LinkConfigField]:
    episode = LinkConfigField(
        name="episode", label="起播集", type="integer", default=1, source="application_default"
    )
    if kind in {"jiashu", "wangyan", "minglue"}:
        return [episode]
    if kind == "duiba":
        return [
            episode,
            LinkConfigField(
                name="card_point_episode",
                label="卡点集",
                type="integer",
                source="per_drama",
                rule="按剧免费集数",
            ),
        ]
    if kind == "gangganhao":
        return [
            LinkConfigField(
                name="free_episode_count",
                label="免费集数",
                type="integer",
                source="per_drama",
                rule="按剧免费集数，缺失时为 3",
                minimum=0,
            ),
            LinkConfigField(
                name="episode_seq",
                label="跳转集",
                type="integer",
                default=1,
                source="application_default",
            ),
            LinkConfigField(
                name="payment_template_id",
                label="支付模板",
                type="select",
                required=payment_template_required(application),
                options=_template_options(application),
                source="application_default",
            ),
        ]
    if kind == "rongliang":
        # 容量后台新增链接只允许选择合集；剧集与别名由网页自动填写，这里保持一致。
        return []
    raise ValueError("unknown provider kind")


def validate_link_values(
    kind: str,
    application: Any,
    values: dict[str, Any],
    *,
    target: Literal["defaults", "overrides"],
) -> dict[str, Any]:
    """校验应用默认值或本次调整；None、空字符串表示未设置，直接丢弃。"""

    if not isinstance(values, dict):
        raise _invalid()
    fields = {field.name: field for field in _fields(kind, application)}
    result: dict[str, Any] = {}
    for name, raw in values.items():
        field = fields.get(name)
        if field is None or (target == "defaults" and field.source != "application_default"):
            raise _invalid(f"不支持的取链参数：{name}")
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool):
            raise _invalid(f"{field.label}必须是整数")
        try:
            number = int(str(raw))
        except ValueError:
            raise _invalid(f"{field.label}必须是整数") from None
        if str(number) != str(raw).strip() or number < field.minimum:
            raise _invalid(f"{field.label}必须是不小于 {field.minimum} 的整数")
        # 有版权方返回的选项时只接受选项内的值；没有选项时无法本地核实，交给建链接口校验。
        if (
            field.type == "select"
            and field.options
            and str(number) not in {option.value for option in field.options}
        ):
            raise _invalid(f"{field.label}不在版权方可选范围内")
        result[name] = number
    return result


def effective_link_config(kind: str, application: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """本次调整覆盖应用默认值，再补系统默认；按剧字段未调整时不写入，留给取链逐剧计算。"""

    fields = _fields(kind, application)
    merged = {
        field.name: field.default
        for field in fields
        if field.source == "application_default" and field.default is not None
    }
    merged.update(
        validate_link_values(
            kind, application, getattr(application, "link_defaults", None) or {}, target="defaults"
        )
    )
    merged.update(validate_link_values(kind, application, overrides, target="overrides"))
    if (
        kind == "gangganhao"
        and payment_template_required(application)
        and "payment_template_id" not in merged
    ):
        raise _invalid(
            "付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置",
            "link_config_template_required",
        )
    return merged


def capabilities_for_kind(provider_kind: str, application: Any = None) -> LinkConfigSchema:
    """Return the safe, UI-facing link configuration contract for a provider."""

    fields = _fields(provider_kind, application)
    return LinkConfigSchema(
        provider_kind=provider_kind,
        fields=fields,
        defaults={field.name: field.default for field in fields if field.default is not None},
        application_defaults=dict(getattr(application, "link_defaults", None) or {}),
        schema_version=2,
    )
