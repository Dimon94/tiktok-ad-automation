"""每接口固定大小的后台机会与扫描状态；不混入HTTP成本余额。"""

from datetime import UTC, datetime
from uuid import UUID

from app.modules.sync_control.directory_candidates import DiscoveryCursor

_SCAN = "scan:"
_ACCOUNT = "account:"


def completion_opportunity(policy: dict[str, float]) -> bool:
    return int(policy.get("turn", 0)) % 3 != 0


def last_completion_account(policy: dict[str, float]) -> str | None:
    return next((k[len(_ACCOUNT) :] for k in policy if k.startswith(_ACCOUNT)), None)


def completion_scan(policy: dict[str, float], now: datetime) -> DiscoveryCursor:
    excluded = last_completion_account(policy)
    saved_excluded = next(
        (k[len("scan:exclude:") :] for k in policy if k.startswith("scan:exclude:")),
        None,
    )
    if saved_excluded != excluded or "scan:until" not in policy:
        reset_scan(policy)
        return DiscoveryCursor.start(now)
    scope = next(
        (UUID(k[len("scan:scope:") :]) for k in policy if k.startswith("scan:scope:")),
        UUID(int=0),
    )
    return DiscoveryCursor(
        datetime.fromtimestamp(policy["scan:at"], UTC),
        scope,
        datetime.fromtimestamp(policy["scan:until"], UTC),
        datetime.min.replace(tzinfo=UTC),
        UUID(int=0),
    )


def save_scan(
    policy: dict[str, float], cursor: DiscoveryCursor, *, include_last: bool = False
) -> None:
    reset_scan(policy)
    policy["scan:at"], policy["scan:until"] = (
        cursor.at.timestamp(),
        cursor.until.timestamp(),
    )
    policy[f"scan:scope:{cursor.scope_id}"] = 1.0
    if include_last:
        policy["scan:include_last"] = 1.0
    excluded = last_completion_account(policy)
    if excluded is not None:
        policy[f"scan:exclude:{excluded}"] = 1.0


def reset_scan(policy: dict[str, float]) -> None:
    for key in list(policy):
        if key.startswith(_SCAN):
            del policy[key]


def record_background_grant(
    policy: dict[str, float], *, advertiser_id: str, completion: bool
) -> None:
    policy["turn"] = float((int(policy.get("turn", 0)) + 1) % 3)
    if completion:
        for key in list(policy):
            if key.startswith(_ACCOUNT):
                del policy[key]
        policy[_ACCOUNT + advertiser_id] = 1.0
        reset_scan(policy)
    # 普通成功/fallback不清扫描进度，避免反复从同一个无资格前缀开始。
