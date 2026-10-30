"""冻结值合同：执行租约不是平台事实版本，优先级也不是需求身份。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class WaitReason(StrEnum):
    FUTURE_SCHEDULE = "FUTURE_SCHEDULE"
    QUOTA = "QUOTA"
    PROVIDER_COOLDOWN = "PROVIDER_COOLDOWN"
    FAIR_BUDGET = "FAIR_BUDGET"
    CAPACITY = "CAPACITY"
    DEPENDENCY = "DEPENDENCY"
    RETRY = "RETRY"


@dataclass(frozen=True)
class ScopeKey:
    digest: str
    tenant_id: UUID
    actor_id: UUID
    advertiser_id: str
    resource: str


@dataclass(frozen=True)
class BucketKey:
    key: str
    local_date: date | None = None


@dataclass(frozen=True)
class RequirementRevision:
    requirement_id: UUID
    scope_id: UUID
    revision: int
    requested_after: datetime


@dataclass(frozen=True)
class ExecutionFence:
    execution_id: UUID
    execution_kind: str
    generation: int
    owner_epoch: int


@dataclass(frozen=True)
class PublicationVersion:
    observed_at: datetime
    version: int

    def __post_init__(self) -> None:
        if self.observed_at.utcoffset() is None or self.version < 1:
            raise ValueError(
                "publication requires an aware observation and positive version"
            )
