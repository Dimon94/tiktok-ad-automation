"""广告管理阶段 C 的通道无关输入/输出合同。

这些 DTO 在生成预览时冻结数值和影响范围。百分比只在 ``MutationSpec``
被解释一次，任务重试消费保存的最终值，避免重复计算带来的漂移。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.reporting.schemas import FrozenSelection

MutationField = Literal["roas", "budget", "status", "name"]
MutationMode = Literal["set", "add", "subtract", "increase_percent", "decrease_percent"]
StatusValue = Literal["ENABLE", "DISABLE"]
RenameKind = Literal["replace", "affix", "explicit"]
# 表格粘贴改名的单次行数上限（规格约定）。
MAX_RENAME_ROWS = 500


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("management values must be finite decimals")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("management values must be finite decimals") from exc
    if not result.is_finite():
        raise ValueError("management values must be finite decimals")
    return result


class RenameRule(BaseModel):
    """改名规则；名称在预览时按规则逐条算出并冻结，重试不再重新计算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RenameKind
    find: str | None = None
    replace_with: str = ""
    prefix: str = ""
    suffix: str = ""
    # 表格粘贴：推广系列 ID → 新名称。
    names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self) -> RenameRule:
        if self.kind == "replace" and not self.find:
            raise ValueError("replace requires find text")
        if self.kind == "affix" and not (self.prefix or self.suffix):
            raise ValueError("affix requires prefix or suffix")
        if self.kind == "explicit" and not 1 <= len(self.names) <= MAX_RENAME_ROWS:
            raise ValueError("explicit rename requires 1-500 rows")
        return self


class MutationSpec(BaseModel):
    """一项批量管理动作；父级和排除集合都是显式输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MutationField
    mode: MutationMode
    # 改名动作没有数值，由 rename 规则给出最终名称，因此 value 可空。
    value: Decimal | StatusValue | None = None
    rename: RenameRule | None = None
    include_parents: tuple[EntityRef, ...] = ()
    excluded_refs: tuple[EntityRef, ...] = ()
    excluded_material_uses: tuple[MaterialUseRef, ...] = ()
    # 自动规则的提高必须有上限、降低必须有下限；计算预览最终值时钳制。
    min_value: Decimal | None = None
    max_value: Decimal | None = None

    @field_validator("min_value", "max_value", mode="before")
    @classmethod
    def normalize_bound(cls, value: Any) -> Decimal | None:
        return None if value is None else _decimal(value)

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: Any) -> Decimal | str | None:
        if value is None:
            return None
        if value in ("ENABLE", "DISABLE"):
            return str(value)
        return _decimal(value)

    @model_validator(mode="after")
    def validate_operation(self) -> MutationSpec:
        if self.field == "name":
            if self.mode != "set" or self.rename is None or self.value is not None:
                raise ValueError("rename only supports set with a rename rule")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("rename does not take bounds")
            return self
        if self.rename is not None:
            raise ValueError("only name field takes a rename rule")
        if self.value is None:
            raise ValueError("management value required")
        if self.field == "status":
            if self.mode != "set" or self.value not in {"ENABLE", "DISABLE"}:
                raise ValueError("status only supports set ENABLE or DISABLE")
        elif isinstance(self.value, str):
            raise ValueError("numeric fields require a decimal value")
        elif self.mode == "set" and self.value <= 0:
            raise ValueError("numeric values must be positive")
        elif (
            self.mode in {"add", "subtract", "increase_percent", "decrease_percent"}
            and self.value <= 0
        ):
            raise ValueError("mutation delta must be positive")
        if self.max_value is not None and (
            self.mode not in {"add", "increase_percent"} or self.max_value <= 0
        ):
            raise ValueError("max_value only bounds positive increases")
        if self.min_value is not None and (
            self.mode not in {"subtract", "decrease_percent"} or self.min_value <= 0
        ):
            raise ValueError("min_value only bounds positive decreases")
        if len(set(self.include_parents)) != len(self.include_parents):
            raise ValueError("duplicate included parent")
        if len(set(self.excluded_refs)) != len(self.excluded_refs):
            raise ValueError("duplicate excluded reference")
        if len(set(self.excluded_material_uses)) != len(self.excluded_material_uses):
            raise ValueError("duplicate excluded material usage")
        return self


class ManagementCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected: int = Field(default=0, ge=0)
    targets: int = Field(default=0, ge=0)
    linked: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)
    refresh_pending: int = Field(default=0, ge=0)
    accepted: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
    not_sent: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    prepared: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class ManagementAttemptPublic(BaseModel):
    """可审计的单次 provider 请求回执摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(ge=1)
    request_at: datetime
    outcome: Literal["ACCEPTED", "REJECTED", "NOT_SENT", "UNKNOWN"]
    request_id: str | None = None
    retryable: bool = False


class BudgetSourcePublic(BaseModel):
    ref: EntityRef
    reason: Literal[
        "owner_selected",
        "campaign_budget_owner",
        "adgroup_budget_owner",
        "campaign_adgroups_expanded",
    ]


class BudgetFactsPublic(BaseModel):
    currency: str
    sources: tuple[BudgetSourcePublic, ...]
    affected_refs: tuple[EntityRef, ...]


class ManagementItemPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: EntityRef
    # 对象名称只用于界面展示；任务项不保存名称，此时为空。
    name: str | None = None
    material_use: MaterialUseRef | None = None
    # 数值动作为原数值；改名动作为原系列名。
    original_value: Decimal | str | None = None
    final_value: Decimal | str | None = None
    budget_facts: BudgetFactsPublic | None = None
    reason: str | None = None
    execution_result: Literal[
        "PENDING",
        "ACCEPTED",
        "REJECTED",
        "NOT_SENT",
        "UNKNOWN",
        "NO_CHANGE",
        "CONFLICT",
        "UNSUPPORTED",
        "CANCELLED",
    ] = "PENDING"
    observation_state: str | None = None
    delivery_status: str | None = None
    request_attribution: str | None = None
    attempts: tuple[ManagementAttemptPublic, ...] = ()
    # 改名预览专用：新名称解析出的剧名/渠道，以及剧归类是否因改名变化。
    drama_name: str | None = None
    provider_label: str | None = None
    drama_changed: bool | None = None


class ManagementPreviewPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preview_id: UUID
    digest: str
    created_at: datetime
    expires_at: datetime
    bc_id: str
    route: FrozenTikTokRoute
    items: tuple[ManagementItemPublic, ...] = ()
    counts: ManagementCounts = Field(default_factory=ManagementCounts)


class ManagementTaskMutation(BaseModel):
    """任务提交时冻结的动作摘要，供操作记录展示“做了什么”。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MutationField
    mode: MutationMode
    value: Decimal | StatusValue | None = None
    rename_kind: RenameKind | None = None


class ManagementTaskSource(BaseModel):
    """任务来源：人工在报表里提交，或由自动规则提交（规则删除后仍标记为规则）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["manual", "rule"]
    rule_id: UUID | None = None
    rule_name: str | None = None


class ManagementTaskPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    items_total: int = 0
    items_offset: int = 0
    next_items_offset: int | None = None
    preparation_error: str | None = None
    preparation_problems: list[dict[str, str]] = Field(default_factory=list)
    bc_id: str
    # 以下摘要只在列表与详情读取时填充；提交接口的即时回执不带。
    created_at: datetime | None = None
    actor_name: str | None = None
    mutation: ManagementTaskMutation | None = None
    source: ManagementTaskSource | None = None
    # 各层级的目标对象数，例如 {"adgroup": 3}。
    target_kinds: dict[str, int] = Field(default_factory=dict)
    status: Literal[
        "PREPARING",
        "READY",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "NEEDS_REVIEW",
        "CANCELLED",
    ]
    counts: ManagementCounts = Field(default_factory=ManagementCounts)
    items: tuple[ManagementItemPublic, ...] = ()


class ManagementPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    mutation: MutationSpec


class ManagementTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: UUID
    preview_digest: str = Field(min_length=64, max_length=64)
    idempotency_key: UUID


class ManagementRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID


class RenameRowInput(BaseModel):
    """表格粘贴的一行；新名称在此不校验，统一由改名预览判定。"""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    new_name: str


class RenameSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bc_id: str = Field(min_length=1, max_length=128)
    rows: list[RenameRowInput] = Field(min_length=1, max_length=MAX_RENAME_ROWS)


class RenameRowProblem(BaseModel):
    campaign_id: str
    # 回显该行粘贴的新名称：重复 ID 的后续行要显示它自己的名称而不是首行的。
    new_name: str
    reason: Literal["id_invalid", "not_found", "duplicate_campaign_id"]


class RenameSelectionPublic(BaseModel):
    selection: FrozenSelection | None
    problems: list[RenameRowProblem]


class DirectManagementRequest(ManagementPreviewRequest):
    idempotency_key: UUID
    row_problems: list[RenameRowProblem] = Field(
        default_factory=list, max_length=MAX_RENAME_ROWS
    )
