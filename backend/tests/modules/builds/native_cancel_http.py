"""本地取消 HTTP 权限与重复请求竞争；不调用平台。"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api.deps import get_current_user, get_db
from app.core.errors import DomainError, domain_error_handler
from app.modules.builds.recovery_api import router


def cancel_twice(engine, context, submission_id):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DomainError, domain_error_handler)

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=context.actor_id
    )
    url = f"/tenants/{context.tenant_id}/submissions/{submission_id}/cancel"

    def cancel(_):
        with TestClient(app) as client:
            return client.post(url)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(cancel, range(2)))
    assert all(r.status_code == 200 for r in replies)
    assert all(r.json()["submission_id"] == str(submission_id) for r in replies)
    with TestClient(app) as client:
        assert client.post(
            f"/tenants/{uuid4()}/submissions/{submission_id}/cancel"
        ).status_code in {403, 404}
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    with TestClient(app) as client:
        assert client.post(url).status_code in {403, 404}
