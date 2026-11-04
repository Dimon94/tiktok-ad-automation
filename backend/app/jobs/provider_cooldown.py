"""平台冷却按实际应用/接口共享；未知报表拒绝按探测轮逐级退避。"""

from typing import Any
from uuid import UUID

_RECORD = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1])*1000 + math.floor(tonumber(stamp[2])/1000)
local delay = tonumber(ARGV[3])
if ARGV[4] == '1' then
    local score = tonumber(redis.call('ZSCORE',KEYS[3],ARGV[1]))
    if not score then
        -- 无法证明请求所属轮次时保守等待，不能无协调地放开短试探。
        delay = math.max(delay,300000)
    else
        local started = score-tonumber(ARGV[2])
        local attempts = tonumber(redis.call('HGET',KEYS[2],'attempts')) or 0
        local cutoff = tonumber(redis.call('HGET',KEYS[2],'cutoff')) or 0
        if attempts == 0 or started > cutoff then
            attempts = math.min(attempts+1,6)
            redis.call('HSET',KEYS[2],'attempts',attempts,'cutoff',now)
        end
        -- 已在途的迟到失败属于原轮次，不能把同一限流波连续升级六次。
        redis.call('PEXPIRE',KEYS[2],900000)
        local waits = {5000,10000,20000,40000,60000,300000}
        delay = math.max(delay,waits[attempts])
    end
end
local previous = tonumber(redis.call('GET',KEYS[1])) or 0
local until_at = math.max(previous,now+delay)
redis.call('SET',KEYS[1],tostring(until_at),'PX',math.ceil(until_at-now))
return math.ceil(until_at-now)
"""


def record_provider_cooldown(
    redis_client: Any,
    *,
    admission_keys: list[str],
    lease_id: UUID,
    lease_ms: int,
    delay_ms: int,
    adaptive_probe: bool,
) -> int:
    """错误路径一次原子更新；正常请求不增加Redis往返，也不改变已发送次数。"""
    cooldown = admission_keys[1] + ":cooldown"
    return int(
        redis_client.eval(
            _RECORD,
            3,
            cooldown,
            cooldown + ":probe",
            admission_keys[2],
            str(lease_id),
            lease_ms,
            delay_ms,
            int(adaptive_probe),
        )
    )
