"""领取时重新签发原时长的短期执行授权，排队不消耗模型分析窗口。"""

from datetime import datetime, timedelta
from math import ceil


def claim_expiry(
    *,
    created_at: datetime,
    expires_at: datetime,
    restart_generation: int,
    now: datetime,
) -> datetime:
    # 首次保留用户请求的 30..900 秒；恢复轮沿用既有 restart_task 的五分钟。
    seconds = (
        300
        if restart_generation
        else max(30, min(900, ceil((expires_at - created_at).total_seconds())))
    )
    return now + timedelta(seconds=seconds)


MAX_SESSION_SECONDS = 2 * 60 * 60


def heartbeat_expiry(*, started_at: datetime, now: datetime) -> datetime:
    """分析可分批持续，短令牌每次续五分钟，但单会话最多两小时。"""
    from app.core.errors import DomainError

    deadline = started_at + timedelta(seconds=MAX_SESSION_SECONDS)
    if now >= deadline:
        raise DomainError("ai_dsh_authorization_expired", "复盘会话已到最长执行期限")
    return min(now + timedelta(minutes=5), deadline)
