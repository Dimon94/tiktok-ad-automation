"""仅测试宿主：生产路由/真实数据库，认证依赖绑定隔离操作者。"""

import os
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from sqlmodel import Session

from app.api.deps import get_current_user, get_db
from app.core.errors import DomainError, domain_error_handler
from app.modules.builds.api import router as previews_router
from app.modules.builds.submission_api import router as submission_router


def submit_in_browser(
    engine, context, bc_id, preview_id, *, draft_id=None, mode="submit"
):
    app = FastAPI()
    app.include_router(previews_router, prefix="/api")
    app.include_router(submission_router, prefix="/api")
    app.add_exception_handler(DomainError, domain_error_handler)

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=context.actor_id
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        result = subprocess.run(
            [
                "bun",
                "run",
                "test",
                "--project=workspace",
                "tests/build-preview.spec.ts"
                if mode in {"submit", "batch"}
                else "tests/build-preparation.spec.ts",
                "-g",
                "@native-batch-live"
                if mode == "batch"
                else "@native-live"
                if mode == "submit"
                else "@native-plan-live",
                "--reporter=line",
            ],
            cwd=Path(__file__).resolve().parents[4] / "frontend",
            env={
                **os.environ,
                "NATIVE_TEST_HOST": f"http://127.0.0.1:{port}",
                "NATIVE_TEST_TENANT": str(context.tenant_id),
                "NATIVE_TEST_BC": bc_id,
                "NATIVE_TEST_PREVIEW": str(preview_id),
                "NATIVE_TEST_DRAFT": str(draft_id),
                "NATIVE_PLAN_MODE": mode,
                "PLAYWRIGHT_PORT": "5197" if mode in {"submit", "batch"} else "5198",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
