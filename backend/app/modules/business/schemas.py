from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RateInput(Input):
    kind: Literal["provider", "rebate"]
    scope_id: str = Field(min_length=1, max_length=128)
    effective_date: date
    rate: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=8)


class RatePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    scope_id: str
    effective_date: date
    rate: Decimal
    revision: int


class MappingInput(Input):
    bc_id: str = Field(min_length=1, max_length=128)
    advertiser_id: str = Field(min_length=1, max_length=128)
    campaign_id: str = Field(min_length=1, max_length=128)
    drama_id: UUID


class RebuildInput(Input):
    bc_id: str = Field(min_length=1, max_length=128)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def dates(self) -> Self:
        if (
            self.start_date > self.end_date
            or (self.end_date - self.start_date).days > 3650
        ):
            raise ValueError("日期范围无效或超过十年")
        return self


class DashboardFilter(RebuildInput):
    dimension: Literal["campaign", "drama", "provider", "day"] = "drama"
    scenario: Literal["base", "comic", "other", "existing"] = "base"
    advertiser_ids: list[str] = Field(default_factory=list, max_length=1000)
    campaign_id: str | None = Field(default=None, max_length=128)
    drama_key: str | None = Field(default=None, max_length=255)
    drama_id: UUID | None = None
    provider_id: UUID | None = None
    unmapped_only: bool = False
    currency: str = Field(default="USD", min_length=1, max_length=16)
    query: str = Field(default="", max_length=255)
    sort_by: Literal[
        "name",
        "spend",
        "revenue",
        "provider_share",
        "net_revenue",
        "rebate",
        "platform_roas",
        "business_roas",
        "business_revenue",
        "compensation",
        "subsidy_comic",
        "subsidy_other",
        "subsidy_existing",
    ] = "spend"
    descending: bool = True
    offset: int = Field(default=0, ge=0, le=10000000)
    limit: int = Field(default=50, ge=1, le=200)
    run_ids: list[UUID] | None = Field(default=None, max_length=100)


class BusinessRow(BaseModel):
    key: str
    name: str
    provider_name: str = ""
    provider_id: UUID | None = None
    drama_id: UUID | None = None
    advertiser_id: str | None = None
    campaign_id: str | None = None
    values: dict[str, Decimal | None]


class DashboardPublic(BaseModel):
    rows: list[BusinessRow]
    totals: dict[str, Decimal | None]
    total: int
    run_ids: list[UUID]
    currency: str
    end_date: date
    currencies: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None
    issues: list[str] = Field(default_factory=list)
    missing_dates: list[date] = Field(default_factory=list)
    pending: bool = False


class ExportPublic(BaseModel):
    id: UUID
    status: str
    expires_at: datetime


class EvidenceInput(Input):
    bc_id: str = Field(min_length=1, max_length=128)
    advertiser_id: str = Field(min_length=1, max_length=128)
    campaign_id: str = Field(min_length=1, max_length=128)
    stat_date: date
    bid: Decimal | None = Field(default=None, gt=0)
    eligible: bool | None = None
    source: str = Field(min_length=1, max_length=1000)


class EvidencePublic(BaseModel):
    bid: Decimal | None = None
    eligible: bool | None = None
    source: str = ""


class MemberInput(Input):
    bc_id: str = Field(min_length=1, max_length=128)
    advertiser_id: str = Field(min_length=1, max_length=128)
    start_date: date
    end_date: date | None = None
