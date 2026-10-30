"""筛查页先冻结请求，退出规划事务后读取，再凭原状态指纹消费回包。"""

import copy
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

from app.modules.reporting.store_models import AdSyncBc

_RESPONSES: ContextVar[dict[str, Any] | None] = ContextVar(
    "sync_screen_responses", default=None
)


@contextmanager
def screening_responses(responses: dict[str, Any]) -> Iterator[None]:
    token = _RESPONSES.set(responses)
    try:
        yield
    finally:
        _RESPONSES.reset(token)


class ScreeningReadRequired(Exception):
    def __init__(
        self,
        *,
        plan: AdSyncBc,
        fingerprint: str,
        method: str,
        arguments: dict[str, Any],
    ) -> None:
        self.plan = SimpleNamespace(
            tenant_id=plan.tenant_id,
            bc_id=plan.bc_id,
            actor_id=plan.actor_id,
            frozen_route=copy.deepcopy(plan.frozen_route),
        )
        self.fingerprint, self.method, self.arguments = fingerprint, method, arguments


class ScreeningReader:
    request_count = 0

    def __init__(self, plan: AdSyncBc) -> None:
        self.plan, self.responses = plan, _RESPONSES.get() or {}

    def __getattr__(self, name: str) -> Any:
        def read(**arguments: Any) -> Any:
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        self.plan.frozen_route,
                        self.plan.screening_progress,
                        name,
                        arguments,
                    ],
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            if fingerprint not in self.responses:
                raise ScreeningReadRequired(
                    plan=self.plan,
                    fingerprint=fingerprint,
                    method=name,
                    arguments=arguments,
                )
            response = self.responses[fingerprint]
            if isinstance(response, Exception):
                raise response
            return response

        return read
