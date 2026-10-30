"""调度只观察原准入账本；发送次数仍只能由 admission.lua 消费。"""

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError

from app.core.errors import DomainError
from app.jobs.admission import AdmissionPolicy, admission_keys

_OBSERVE = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1])*1000 + math.floor(tonumber(stamp[2])/1000)
local window = tonumber(ARGV[1])
local capacity, wait, reason = 2147483647, 0, 0
local remaining = {}
for i=1,6 do
    local rate = i <= 2
    local cutoff = rate and now-window or now
    local used = redis.call('ZCOUNT', KEYS[i], '('..cutoff, '+inf')
    local available = math.max(0, tonumber(ARGV[i+1])-used)
    remaining[i] = available
    capacity = math.min(capacity, available)
    if available == 0 then
        local first = redis.call('ZRANGEBYSCORE', KEYS[i], '('..cutoff, '+inf', 'WITHSCORES', 'LIMIT', 0, 1)
        local delay = tonumber(first[2])-now
        if rate then delay = delay+window else delay = math.min(delay,1000) end
        if delay >= wait then wait,reason = math.max(1,delay), rate and 1 or 2 end
    end
end
local minute, day = tonumber(ARGV[8]), tonumber(ARGV[9])
local minute_remaining, day_remaining = 2147483647, 2147483647
if minute > 0 then
    local used = redis.call('ZCOUNT', KEYS[7], '('..(now-60000), '+inf')
    capacity = math.min(capacity,math.max(0,minute-used))
    minute_remaining = math.max(0,minute-used)
    if used >= minute then
        local first = redis.call('ZRANGEBYSCORE', KEYS[7], '('..(now-60000), '+inf', 'WITHSCORES', 'LIMIT', 0, 1)
        local delay = tonumber(first[2])+60000-now
        if delay >= wait then wait,reason = math.max(1,delay),1 end
    end
end
if day > 0 then
    local utcday = math.floor(now/86400000)
    local used = tonumber(redis.call('HGET',KEYS[8],tostring(utcday))) or 0
    capacity = math.min(capacity,math.max(0,day-used))
    day_remaining = math.max(0,day-used)
    if used >= day then
        local delay = (utcday+1)*86400000-now
        if delay >= wait then wait,reason = delay,1 end
    end
end
local cooldown = tonumber(redis.call('GET',KEYS[9])) or 0
if cooldown > now then capacity,wait,reason = 0,math.max(wait,cooldown-now),3 end
return {capacity,math.ceil(wait),reason,now}
"""


@dataclass(frozen=True)
class EndpointReadiness:
    capacity_hint: int
    retry_after_ms: int
    reason: str | None
    observed_at_ms: int


def endpoint_readiness(
    redis: Redis,
    *,
    policy: AdmissionPolicy,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    rate_group: str | None = None,
) -> EndpointReadiness:
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
    keys.extend((keys[1] + ":minute", keys[1] + ":day", keys[1] + ":cooldown"))
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
    ]
    try:
        capacity, delay, reason, now = cast(
            list[int],
            redis.eval(_OBSERVE, len(keys), *keys, *(str(arg) for arg in args)),
        )
    except RedisError as error:
        raise DomainError(
            "admission_unavailable", "调用配额服务暂不可用", True
        ) from error
    return EndpointReadiness(
        int(capacity),
        int(delay),
        (None, "QUOTA", "CAPACITY", "PROVIDER_COOLDOWN")[int(reason)],
        int(now),
    )
