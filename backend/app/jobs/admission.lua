local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local member, window, lease = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
-- 派发占位只约束本机排队，不消费次数；实际准入原子交接且拒绝也释放占位。
if ARGV[13] then
    for i=17,20 do redis.call('ZREM',KEYS[i],ARGV[13]) end
end
local wait, reason = 0, 0
if KEYS[16] then
    local until_at = tonumber(redis.call('GET', KEYS[16])) or 0
    if until_at > now then wait, reason = until_at - now, 9 end
end
for i = 1, 6 do
    local rate = i <= 2
    local cutoff = rate and now - window or now
    for _, n in ipairs({i, i + 6}) do
        redis.call('ZREMRANGEBYSCORE', KEYS[n], '-inf', cutoff)
    end
    local capacity = tonumber(ARGV[i + 3])
    if redis.call('ZSCORE', KEYS[i], member) or redis.call('ZCARD', KEYS[i]) >= capacity then
        local first = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        local remaining = tonumber(first[2]) - now
        if rate then remaining = remaining + window else remaining = math.min(remaining, 1000) end
        if remaining >= wait then wait, reason = math.max(1, remaining), i end
    end
end
-- 分钟为滚动窗口，日额度按UTC日；先检查所有额度，再统一消费，拒绝不扣次。
local minute_limit, day_limit = tonumber(ARGV[11]) or 0, tonumber(ARGV[12]) or 0
local utc_day = math.floor(now / 86400000)
if minute_limit > 0 then
    redis.call('ZREMRANGEBYSCORE', KEYS[14], '-inf', now - 60000)
    if redis.call('ZCARD', KEYS[14]) >= minute_limit then
        local first = redis.call('ZRANGE', KEYS[14], 0, 0, 'WITHSCORES')
        local delay = tonumber(first[2]) + 60000 - now
        if delay >= wait then wait, reason = math.max(1, delay), 7 end
    end
end
if day_limit > 0 then
    local used = tonumber(redis.call('HGET', KEYS[15], tostring(utc_day))) or 0
    if used >= day_limit then
        local delay = (utc_day + 1) * 86400000 - now
        if delay >= wait then wait, reason = math.max(1, delay), 8 end
    end
end
-- 服务分类仅记录实际使用量，不能因另一类曾经请求过就闲置仍可用的容量。
local metric = KEYS[13]
if wait > 0 then
    redis.call('HINCRBY', metric, 'local_denied:' .. tostring(reason) .. ':' .. ARGV[10], 1)
    redis.call('EXPIRE', metric, 172800)
    return {0, math.ceil(wait), reason}
end
if minute_limit > 0 then
    redis.call('ZADD', KEYS[14], now, member)
    redis.call('PEXPIRE', KEYS[14], 61000)
end
if day_limit > 0 then
    redis.call('HDEL', KEYS[15], tostring(utc_day - 1))
    redis.call('HINCRBY', KEYS[15], tostring(utc_day), 1)
    redis.call('PEXPIRE', KEYS[15], (utc_day + 1) * 86400000 - now + 1000)
end
redis.call('HINCRBY', metric, 'granted:' .. ARGV[10], 1)
redis.call('EXPIRE', metric, 172800)
for i = 1, 6 do
    local rate = i <= 2
    local score = rate and now or now + lease
    for _, n in ipairs({i, i + 6}) do
        redis.call('ZADD', KEYS[n], score, member)
        local latest = redis.call('ZRANGE', KEYS[n], -1, -1, 'WITHSCORES')
        local ttl = rate and window + 1000 or math.ceil(tonumber(latest[2]) - now + 1000)
        redis.call('PEXPIRE', KEYS[n], ttl)
    end
end
return {1, 0, 0}
