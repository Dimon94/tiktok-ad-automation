"""Redis-wide SDK request admission; never use a connection ID as app_scope.

Every actual SDK request needs a fresh lease_id. Denial is a rescheduling signal,
not a reason to sleep in a worker. Release in the SDK caller's finally block;
log release failures separately so they cannot overwrite a successful result or
turn it into a blindly retryable request. Configure leases longer than the SDK's
connect/read timeouts plus processing margin.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.errors import DomainError

_ADMISSION_SCRIPT = Path(__file__).with_suffix(".lua").read_text()
_RELEASE_SCRIPT = "for i=1,#KEYS do redis.call('ZREM',KEYS[i],ARGV[1]) end return 1"
_ENDPOINT_OVERRIDES = frozenset(
    {
        "endpoint_max_inflight",
        "endpoint_calls_per_window",
        "lease_ms",
        "endpoint_calls_per_minute",
        "endpoint_calls_per_day",
    }
)


Workload = Literal["live", "background"]


@dataclass(frozen=True)
class Admission:
    granted: bool
    retry_after_ms: int
    reason: str | None = None


class AdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    app_max_inflight: int = Field(gt=0)
    endpoint_max_inflight: int = Field(gt=0)
    tenant_max_inflight: int = Field(gt=0)
    advertiser_max_inflight: int = Field(gt=0)
    app_calls_per_window: int = Field(gt=0)
    endpoint_calls_per_window: int = Field(gt=0)
    window_ms: int = Field(gt=0)
    lease_ms: int = Field(gt=0)
    endpoint_calls_per_minute: int = Field(default=0, ge=0)
    endpoint_calls_per_day: int = Field(default=0, ge=0)


def admission_policy(
    endpoint: str, *, channel: str | None = None, physical_endpoint: str | None = None
) -> AdmissionPolicy:
    config = settings.TIKTOK_CALL_POLICIES
    if config == {}:
        raise DomainError("admission_unconfigured", "请配置应用调用额度")
    try:
        if (
            not isinstance(config, dict)
            or not config
            or set(config) - {"base", "endpoints", "sdk_level"}
        ):
            raise ValueError("Invalid policy structure")
        base = config.get("base")
        endpoints = config.get("endpoints", {})
        if not isinstance(base, dict) or not isinstance(endpoints, dict):
            raise ValueError("Invalid policy structure")
        # Check every override: a malformed unused endpoint is still a broken
        # deployment configuration, never a reason to silently use base limits.
        for name, override in endpoints.items():
            if (
                not isinstance(name, str)
                or not isinstance(override, dict)
                or set(override) - _ENDPOINT_OVERRIDES
            ):
                raise ValueError("Endpoint cannot override shared quotas")
            AdmissionPolicy.model_validate({**base, **override})
        AdmissionPolicy.model_validate(base)
        from app.integrations.tiktok.platform_quotas import (
            LEVELS,
            sdk_endpoint,
            sdk_quota,
        )

        level = config.get("sdk_level")
        if level is not None and level not in LEVELS:
            raise ValueError("Invalid SDK application level")
        resolved = AdmissionPolicy.model_validate(
            {
                **base,
                **endpoints.get(endpoint, {}),
                **(endpoints.get(f"{channel}:{endpoint}", {}) if channel else {}),
            }
        )
        if channel == "OFFICIAL_API" and level is not None:
            if physical_endpoint is None:
                # 既有调用方可能只取lease_ms，也统一解析已登记的真实SDK接口。
                physical_endpoint = sdk_endpoint(endpoint)
            qps, qpm, qpd = sdk_quota(level, physical_endpoint)
            # 官方平台额度优先于旧的业务名3QPS；物理端点覆盖用于已核实的特殊额度。
            override = endpoints.get(f"OFFICIAL_API:{physical_endpoint}", {})
            resolved = AdmissionPolicy.model_validate(
                {
                    **resolved.model_dump(),
                    "window_ms": 1000,
                    "app_calls_per_window": qps,
                    "endpoint_calls_per_window": qps,
                    "endpoint_calls_per_minute": qpm,
                    "endpoint_calls_per_day": qpd,
                    **override,
                }
            )
        return resolved
    except (ValidationError, TypeError, ValueError) as error:
        raise DomainError("admission_policy_invalid", "调用额度配置无效") from error


def admission_keys(
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    rate_group: str | None = None,
) -> list[str]:
    """共享桶使用相同 Redis hash tag，确保 Lua 原子准入。"""
    digest = sha256(app_scope.encode()).hexdigest()
    base = f"tiktok:{{{digest}}}"
    endpoint_key = sha256(endpoint.encode()).hexdigest()
    account_key = sha256(advertiser_id.encode()).hexdigest()
    keys = [
        f"{base}:rate",
        f"{base}:endpoint:{endpoint_key}:rate",
        f"{base}:active",
        f"{base}:endpoint:{endpoint_key}:active",
        f"{base}:tenant:{tenant_id}:active",
        f"{base}:advertiser:{account_key}:active",
    ]
    if rate_group is not None:
        keys[0] = f"{base}:provider:{sha256(rate_group.encode()).hexdigest()}:rate"
    return keys


def admit_call(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    lease_id: UUID,
    policy: AdmissionPolicy,
    workload: Workload = "live",
    rate_group: str | None = None,
    dispatch_reservation_id: str | None = None,
) -> Admission:
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
    if workload not in ("live", "background"):
        raise DomainError("admission_workload_invalid", "调用服务分类无效")
    # 分类桶只观察用量，不额外预留闲置配额。真实总额由共享桶原子约束。
    keys += [f"{key}:{workload}" for key in keys]
    keys.append(keys[1] + ":metrics")
    keys.extend((keys[1] + ":minute", keys[1] + ":day", keys[1] + ":cooldown"))
    args = [
        str(lease_id),
        policy.window_ms,
        policy.lease_ms,
        policy.app_calls_per_window,
        policy.endpoint_calls_per_window,
        policy.app_max_inflight,
        policy.endpoint_max_inflight,
        policy.tenant_max_inflight,
        policy.advertiser_max_inflight,
        workload,
        policy.endpoint_calls_per_minute,
        policy.endpoint_calls_per_day,
    ]
    if dispatch_reservation_id is not None:
        from app.modules.sync_control.occupancy import occupancy_keys

        keys.extend(
            occupancy_keys(app_scope, endpoint, tenant_id, advertiser_id, rate_group)
        )
        args.append(dispatch_reservation_id)
    try:
        granted, delay, reason = redis_client.eval(
            _ADMISSION_SCRIPT, len(keys), *keys, *args
        )
    except RedisError as error:
        raise DomainError(
            "admission_unavailable", "调用配额服务暂不可用", True
        ) from error
    return Admission(
        bool(granted),
        int(delay),
        (
            None,
            "rate_budget",
            "endpoint_rate",
            "app_inflight",
            "endpoint_inflight",
            "tenant_inflight",
            "advertiser_inflight",
            "endpoint_minute",
            "endpoint_day",
            "provider_cooldown",
        )[int(reason)],
    )


def release_call(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    lease_id: UUID,
) -> None:
    """Release inflight leases only. RedisError is handled separately by caller."""
    # 只释放并发租约，不返还已经消耗的请求次数。
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id)[2:6]
    keys += [f"{key}:{kind}" for kind in ("live", "background") for key in keys]
    redis_client.eval(_RELEASE_SCRIPT, len(keys), *keys, str(lease_id))


@contextmanager
def admitted_scope(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    policy: AdmissionPolicy,
    denied_error: Callable[[int], Exception],
    workload: Workload = "live",
    rate_group: str | None = None,
    admit: Callable[..., Admission] = admit_call,
    release: Callable[..., None] = release_call,
) -> Iterator[None]:
    """两个通道共用租约生命周期；认证和上游配额域由调用边界提供。"""
    lease_id = uuid4()
    scope = {
        "app_scope": app_scope,
        "endpoint": endpoint,
        "tenant_id": tenant_id,
        "advertiser_id": advertiser_id,
        "lease_id": lease_id,
    }
    options = {"rate_group": rate_group} if rate_group is not None else {}
    from app.modules.sync_control.occupancy import reserve_for_send

    reservation, ready = reserve_for_send(
        redis_client,
        policy=policy,
        app_scope=app_scope,
        endpoint=endpoint,
        tenant_id=tenant_id,
        advertiser_id=advertiser_id,
        rate_group=rate_group,
    )
    if ready is not None and ready.capacity_hint == 0:
        error = denied_error(ready.retry_after_ms)
        cast(Any, error).admission_reason = ready.reason
        raise error
    if reservation is not None:
        options["dispatch_reservation_id"] = reservation
    admission = admit(
        redis_client, **scope, policy=policy, workload=workload, **options
    )
    if not admission.granted:
        error = denied_error(admission.retry_after_ms)
        cast(Any, error).admission_reason = admission.reason
        raise error
    interrupted = False
    try:
        yield
    except SystemExit, KeyboardInterrupt, SoftTimeLimitExceeded:
        interrupted = True
        raise
    except Exception as error:
        if getattr(error, "admission_source", None) == "provider":
            # 平台拒绝发生在真实发送之后，与本地未发送的拒绝分开观测。
            provider_error = cast(Any, error)
            original_delay = getattr(provider_error, "retry_after_ms", None)
            if getattr(provider_error, "shared_throttle_probe", False):
                provider_error.retry_after_ms = max(original_delay or 0, 300000)
            try:
                key = (
                    admission_keys(app_scope, endpoint, tenant_id, advertiser_id)[1]
                    + ":metrics"
                )
                redis_client.hincrby(key, f"provider_denied:{workload}", 1)
                redis_client.expire(key, 172800)
                delay = original_delay
                if type(delay) is int and delay > 0:
                    from app.jobs.provider_cooldown import record_provider_cooldown

                    probe = bool(getattr(error, "shared_throttle_probe", False))
                    # Redis协调失败时保留上方已设置的保守延期。
                    provider_error.retry_after_ms = record_provider_cooldown(
                        redis_client,
                        admission_keys=admission_keys(
                            app_scope, endpoint, tenant_id, advertiser_id, rate_group
                        ),
                        lease_id=lease_id,
                        lease_ms=policy.lease_ms,
                        delay_ms=delay,
                        adaptive_probe=probe,
                    )
            except RedisError, DomainError:
                logging.getLogger(__name__).warning("admission_metric_failed")
        raise
    finally:
        # 进程终止不证明远端调用结束，保留原租约至过期；正常释放也不解除业务 UNKNOWN 围栏。
        if not interrupted:
            try:
                cleanup = (
                    redis_client.cleanup_scope()
                    if callable(getattr(type(redis_client), "cleanup_scope", None))
                    else nullcontext(redis_client)
                )
                with cleanup as cleanup_client:
                    release(cleanup_client, **scope)
            except RedisError, DomainError:
                logging.getLogger(__name__).warning(
                    "admission_release_failed",
                    extra={"tenant_id": str(tenant_id), "lease_id": str(lease_id)},
                )
