"""API contracts for tenant AI optimizer configuration."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

Priority = Literal["SPEND", "ROAS", "BALANCED"]
OptimizationMode = Literal["AUTO", "MANUAL"]


class OptimizerFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_spend: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    target_roas: Decimal | None = Field(
        default=None, gt=0, max_digits=38, decimal_places=12
    )
    priority: Priority = "BALANCED"
    bc_id: str = Field(min_length=1, max_length=128)
    advertiser_ids: tuple[str, ...] = Field(min_length=1, max_length=10000)
    optimization_mode: OptimizationMode = "MANUAL"
    timezone: str = "Asia/Shanghai"
    review_time: str = Field(default="13:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    material_config: dict[str, object] = Field(default_factory=dict)
    authorization_limits: dict[str, object] = Field(default_factory=dict)
    strategy_version_id: UUID
    rule_ids: tuple[UUID, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def validate_scope(self) -> OptimizerFields:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("timezone must be an IANA timezone") from None
        if self.material_config:
            from app.modules.ai_workbench.orders import validate_config

            validate_config(self.material_config)
        if len(set(self.advertiser_ids)) != len(self.advertiser_ids):
            raise ValueError("advertiser_ids must be unique")
        if self.priority == "ROAS" and self.target_roas is None:
            raise ValueError("target_roas is required for ROAS priority")
        return self


class OptimizerCreate(OptimizerFields):
    name: str = Field(min_length=1, max_length=120)
    request_id: UUID
    active: bool = True


class OptimizerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    daily_spend: Decimal | None = Field(
        default=None, gt=0, max_digits=38, decimal_places=12
    )
    target_roas: Decimal | None = Field(
        default=None, gt=0, max_digits=38, decimal_places=12
    )
    priority: Priority | None = None
    bc_id: str | None = Field(default=None, min_length=1, max_length=128)
    advertiser_ids: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=10000
    )
    optimization_mode: OptimizationMode | None = None
    timezone: str | None = None
    review_time: str | None = Field(
        default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    material_config: dict[str, object] | None = None
    authorization_limits: dict[str, object] | None = None
    strategy_version_id: UUID | None = None
    rule_ids: tuple[UUID, ...] | None = Field(default=None, max_length=1000)
    active: bool | None = None
    request_id: UUID

    @model_validator(mode="after")
    def require_change(self) -> OptimizerUpdate:
        # ``target_roas: null`` is an intentional clear operation; use field-set
        # semantics instead of treating every nullable value as omission.
        mutation_fields = self.model_fields_set - {"request_id"}
        if not mutation_fields:
            raise ValueError("At least one optimizer field is required")
        invalid_nulls = {
            field
            for field in mutation_fields
            if field != "target_roas" and getattr(self, field) is None
        }
        if invalid_nulls:
            raise ValueError("Optimizer fields cannot be null")
        return self


class OptimizerPublic(OptimizerFields):
    id: UUID
    tenant_id: UUID
    name: str
    active: bool
    version: int
    strategy_binding_id: UUID
    created_by: UUID
    created_at: datetime
    version_created_at: datetime
    request_id: UUID


class OptimizerPage(BaseModel):
    items: list[OptimizerPublic]
    next_cursor: str | None = None
    total: int
