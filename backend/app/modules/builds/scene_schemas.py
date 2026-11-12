"""Immutable, JSON-exportable scene facts; zero means unknown, never a quota."""

from dataclasses import dataclass, field, fields
from typing import Any, Literal, Never
from uuid import UUID

CREATIVE_COMPILER_REVISION = "creative-units-v2-2026-10-29"

SceneResource = Literal[
    "native_series",
    "native_tt_identity",
    "native_bc_identity",
    "account_roles",
    "identity",
    "minis",
    "cta",
    "vbo",
    "regions",
    "budget",
]


class FrozenDict(dict[str, Any]):
    def _deny(self, *args: Any, **kwargs: Any) -> Never:
        raise TypeError("Scene facts are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = (
        __ior__
    ) = _deny


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return str(value) if isinstance(value, UUID) else value


@dataclass(frozen=True)
class SceneContext:
    supported: bool
    reason_codes: tuple[str, ...]
    capability_revision: str
    media_kind: Literal["VIDEO", "CAROUSEL"] = "VIDEO"
    compiler_contract_revision: str = CREATIVE_COMPILER_REVISION
    campaign_fields: dict[str, Any] = field(default_factory=dict)
    adgroup_fields: dict[str, Any] = field(default_factory=dict)
    creative_fields: dict[str, Any] = field(default_factory=dict)
    cta_fields: dict[str, Any] = field(default_factory=dict)
    name_limit: int = 0
    creative_limit: int = 0
    copy_length_limit: int = 0
    evidence_ids: tuple[UUID, ...] = ()
    field_constraints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "campaign_fields",
            "adgroup_fields",
            "creative_fields",
            "cta_fields",
            "field_constraints",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def to_snapshot(self) -> dict[str, Any]:
        return {item.name: _plain(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True)
class SceneRefreshResult:
    evidence_id: UUID | None
    resource: SceneResource
    complete: bool
    next_page: int | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenePreparation:
    job_id: UUID | None
    state: Literal["ready", "queued", "blocked"]
    reason_code: str | None = None
