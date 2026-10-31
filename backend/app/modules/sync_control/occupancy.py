"""本机queued/leased占位，不消费平台次数；实际admit原子交接到inflight。"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast
from uuid import UUID, uuid4

from redis import Redis
from redis.exceptions import RedisError

from app.jobs.admission import AdmissionPolicy, admission_keys
from app.modules.sync_control.readiness import _OBSERVE, EndpointReadiness

_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "sync_dispatch_occupancy", default=None
)
logger = logging.getLogger(__name__)
_TRANSACTION_RESERVATIONS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "sync_transaction_reservations", default=None
)


@contextmanager
def reservation_transaction(redis: Redis) -> Iterator[None]:
    """包住DB事务及commit；失败时只撤销本事务的queued占位，不退已发送额度。"""
    reservations: list[dict[str, Any]] = []
    token = _TRANSACTION_RESERVATIONS.set(reservations)
    try:
        yield
    except BaseException:
        for reservation in reservations:
            try:
                release_dispatch(redis, **reservation)
            except RedisError:
                logger.warning("rolled back dispatch reservation awaits expiry")
        raise
    finally:
        _TRANSACTION_RESERVATIONS.reset(token)


@contextmanager
def dispatch_context(
    redis: Redis | None, *, reservation_id: str | UUID, scope: dict[str, Any]
) -> Iterator[None]:
    state: dict[str, Any] = {
        "reservation_id": str(reservation_id),
        "scope": scope,
        "first": True,
    }
    token = _CURRENT.set(state)
    try:
        yield
    finally:
        # 网关准备/验权失败尚未到HTTP时也释放排队占位。
        try:
            if redis is not None:
                release_dispatch(redis, reservation_id=state["reservation_id"], **scope)
        except RedisError:
            logger.warning("sync dispatch reservation release deferred to expiry")
        finally:
            _CURRENT.reset(token)


def reserve_for_send(
    redis: Redis, *, policy: AdmissionPolicy, **scope: Any
) -> tuple[str | None, EndpointReadiness | None]:
    state = _CURRENT.get()
    if state is None:
        return None, None
    if state["first"]:
        state["first"] = False
        reservation = state["reservation_id"]
        if scope != state["scope"]:
            release_dispatch(redis, reservation_id=reservation, **state["scope"])
    else:
        reservation = str(uuid4())
    ready = reserve_dispatch(redis, reservation_id=reservation, policy=policy, **scope)
    return reservation, ready


_RESERVE = _OBSERVE.replace(
    "return {capacity,math.ceil(wait),reason,now}",
    """
local member, ttl = ARGV[10],tonumber(ARGV[11])
local occupied = {}
for i=10,13 do
    if ARGV[13]=='observe' then
        occupied[i] = redis.call('ZCOUNT',KEYS[i],'('..now,'+inf')
    else
        redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
        occupied[i] = redis.call('ZCARD',KEYS[i])
    end
    if redis.call('ZSCORE',KEYS[i],member) then occupied[i]=occupied[i]-1 end
end
local rate_index = ARGV[12]=='1' and 11 or 10
local map = {rate_index,11,10,11,12,13}
for i=1,6 do capacity=math.min(capacity,math.max(0,remaining[i]-occupied[map[i]])) end
capacity=math.min(capacity,math.max(0,minute_remaining-occupied[11]),math.max(0,day_remaining-occupied[11]))
if capacity <= 0 then
    if wait==0 then wait,reason=1000,2 end
    return {0,math.ceil(wait),reason,now}
end
if ARGV[13]=='observe' then return {capacity,0,0,now} end
for i=10,13 do
    redis.call('ZADD',KEYS[i],now+ttl,member)
    redis.call('PEXPIRE',KEYS[i],ttl+1000)
end
return {capacity,0,0,now}
""",
)


def occupancy_keys(
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    rate_group: str | None = None,
) -> list[str]:
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
    return [key + ":dispatch" for key in keys[2:6]]


def reserve_dispatch(
    redis: Redis,
    *,
    reservation_id: str | UUID,
    policy: AdmissionPolicy,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    rate_group: str | None = None,
    lease_ms: int = 240000,
    _observe: bool = False,
) -> EndpointReadiness:
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
    keys.extend((keys[1] + ":minute", keys[1] + ":day", keys[1] + ":cooldown"))
    keys.extend(
        occupancy_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
    )
    args = [
        policy.window_ms,
        policy.app_calls_per_window,
        policy.endpoint_calls_per_window,
        policy.app_max_inflight,
        policy.endpoint_max_inflight,
        policy.tenant_max_inflight,
        policy.advertiser_max_inflight,
        policy.endpoint_calls_per_minute,
        policy.endpoint_calls_per_day,
        str(reservation_id),
        lease_ms,
        int(rate_group is not None),
        "observe" if _observe else "reserve",
    ]
    capacity, delay, reason, now = cast(
        list[int], redis.eval(_RESERVE, len(keys), *keys, *(str(arg) for arg in args))
    )
    reservations = _TRANSACTION_RESERVATIONS.get()
    if capacity > 0 and not _observe and reservations is not None:
        reservations.append(
            {
                "reservation_id": reservation_id,
                "app_scope": app_scope,
                "endpoint": endpoint,
                "tenant_id": tenant_id,
                "advertiser_id": advertiser_id,
                "rate_group": rate_group,
            }
        )
    return EndpointReadiness(
        int(capacity),
        int(delay),
        (None, "QUOTA", "CAPACITY", "PROVIDER_COOLDOWN")[int(reason)],
        int(now),
    )


def release_dispatch(redis: Redis, *, reservation_id: str | UUID, **scope: Any) -> None:
    keys = occupancy_keys(**scope)
    redis.eval(
        "for i=1,#KEYS do redis.call('ZREM',KEYS[i],ARGV[1]) end return 1",
        len(keys),
        *keys,
        str(reservation_id),
    )


def dispatch_readiness(
    redis: Redis,
    *,
    policy: AdmissionPolicy,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    rate_group: str | None = None,
) -> EndpointReadiness:
    """只读原平台与queued账本，不预占、不计调用、不写拒绝指标。"""
    return reserve_dispatch(
        redis,
        reservation_id="",
        policy=policy,
        app_scope=app_scope,
        endpoint=endpoint,
        tenant_id=tenant_id,
        advertiser_id=advertiser_id,
        rate_group=rate_group,
        _observe=True,
    )
