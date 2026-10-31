"""一次报表步骤只汇总固定阶段；无额外查询，不把阶段返回当成事务提交。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic
from typing import Any, ParamSpec, TypeVar

from app.core.diagnostics import emit, error_fields, identifier

_STAGES = frozenset(
    {
        "step_prepare",
        "claim_commit",
        "execute",
        "read",
        "financial_publish",
        "publish_buckets",
        "publish_partitions",
        "publish_facts",
        "publish_rollup",
        "publish_state",
        "publish_flush",
        "publish_projection",
        "reconciliation",
        "reconciliation_account",
        "directory_refresh",
        "directory_lock",
        "directory_rules",
        "directory_pending",
        "directory_facts_query",
        "directory_candidates",
        "directory_promote",
        "directory_request",
        "links_enqueued",
        "links_asset_candidates",
        "links_asset_resolve",
        "links_spark_candidates",
        "links_spark_resolve",
        "links_needs",
        "links_missing_ids",
        "links_spark_bridges",
        "links_directory_signature",
        "links_wait_dependencies",
        "links_unlinked",
        "links_upsert",
        "links_candidate_ads",
        "links_locate_post",
        "links_resolve_local",
        "links_scan_signature",
        "links_refresh",
        "links_overview_candidates",
        "links_directory_request",
        "directory_verify_routes",
        "directory_slot_lock",
        "directory_existing",
        "directory_flush",
        "directory_outbox",
        "job_finalize",
        "job_commit",
        "record_calls",
        "refill",
        "refill_dispatch",
    }
)
_COUNTS = frozenset(
    {
        "rows",
        "buckets",
        "directory_pending",
        "directory_refs",
        "directory_created",
        "refill_jobs",
    }
)
_KINDS = frozenset(
    {"ad_day", "material_day", "material_links", "campaign_hour", "account_day"}
)
_RESULTS = frozenset({"COMPLETE", "WAITING", "FAILED"})
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _Summary:
    stages: dict[str, dict[str, int | float]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    job_kind: str | None = None
    account_count: int | None = None
    job_commit_confirmed: bool = False
    job_commit_result: str | None = None


_current: ContextVar[_Summary | None] = ContextVar(
    "reporting_stage_summary", default=None
)


@contextmanager
def stage(name: str) -> Iterator[None]:
    summary = _current.get()
    if summary is None or name not in _STAGES:
        yield
        return
    started = monotonic()
    failed = 0
    try:
        yield
    except BaseException:
        failed = 1
        raise
    finally:
        elapsed = (monotonic() - started) * 1000
        value = summary.stages.setdefault(
            name, {"calls": 0, "elapsed_ms": 0.0, "max_ms": 0.0, "failures": 0}
        )
        value["calls"] += 1
        value["elapsed_ms"] += elapsed
        value["max_ms"] = max(value["max_ms"], elapsed)
        value["failures"] += failed


def timed(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def counts(**values: Any) -> None:
    summary = _current.get()
    if summary is not None:
        for name, value in values.items():
            if name in _COUNTS and type(value) is int and value >= 0:
                summary.counts[name] = summary.counts.get(name, 0) + value


def set_job_scope(kind: str, account_count: int) -> None:
    summary = _current.get()
    if summary is not None:
        summary.job_kind = kind if kind in _KINDS else None
        if type(account_count) is int and account_count >= 0:
            summary.account_count = account_count


def committed(result: str) -> None:
    # 只在原业务 commit 成功返回后调用；claim 续租不能证明财务发布已提交。
    summary = _current.get()
    if summary is not None and result in _RESULTS:
        summary.job_commit_confirmed = True
        summary.job_commit_result = result


def summarize_job[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        summary = _Summary()
        token = _current.set(summary)
        started = monotonic()
        outcome = "RAISED"
        failure = {}
        try:
            result = function(*args, **kwargs)
            outcome = (
                result if isinstance(result, str) and result in _RESULTS else "UNKNOWN"
            )
            return result
        except BaseException as error:
            failure = error_fields(error)
            raise
        finally:
            _current.reset(token)
            # 各阶段包含其子阶段，不能把重叠计时相加当作总耗时。阶段完成仍可能回滚。
            emit(
                "reporting_stage_summary",
                job_id=identifier(str(kwargs.get("job_id", ""))),
                duration_ms=round((monotonic() - started) * 1000, 3),
                outcome=outcome,
                job_kind=summary.job_kind,
                account_count=summary.account_count,
                job_commit_confirmed=summary.job_commit_confirmed,
                job_commit_result=summary.job_commit_result,
                counts=summary.counts,
                timing_semantics="inclusive",
                stages={
                    name: {
                        key: round(value, 3) if isinstance(value, float) else value
                        for key, value in values.items()
                    }
                    for name, values in summary.stages.items()
                },
                **failure,
            )

    return wrapped
