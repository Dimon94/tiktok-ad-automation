#!/usr/bin/env python3
"""隔离测试库目录发布：预先提交夹具，计时真实COMMIT并跨连接核对可见性。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID


def guard() -> None:
    """压测只允许本机专用库；在应用配置导入及连接前拒绝重定向。"""
    if os.environ.get("APP_ENV_FILE") != "/dev/null":
        raise ValueError("APP_ENV_FILE must be /dev/null")
    parsed = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (
        parsed.scheme != "postgresql+psycopg"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path != "/sync_redesign_test_publish"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("dedicated local publish database required")
    if shutil.disk_usage(Path(__file__).resolve().parent).free < 10 * 1024**3:
        raise ValueError("at least 10 GiB free disk required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-fixture-write", action="store_true")
    parser.add_argument("--background-rows", type=int, default=100000)
    parser.add_argument("--objects", type=int, default=12000)
    parser.add_argument("--materials", type=int, default=40000)
    parser.add_argument("--shared-video", action="store_true")
    parser.add_argument("--reuse-fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        guard()
    except ValueError as error:
        parser.error(str(error))
    if not args.allow_fixture_write:
        parser.error("--allow-fixture-write is required")
    if args.background_rows < 0 or not 1 <= args.objects <= args.materials:
        parser.error("invalid fixture size")
    if args.output.exists():
        parser.error("output already exists; preserve previous evidence")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.core.config import settings
    from tests.database import require_test_database

    require_test_database(str(settings.DATABASE_URL))
    import app.models  # noqa: F401
    from app.core.db import engine
    from app.integrations.tiktok.contracts.ads import (
        AdMaterialUsage,
        CallEvidence,
        DirectoryPage,
        EntityRef,
        MaterialUseRef,
    )
    from app.modules.ads.sync import publish_directory, stage_directory_page
    from sqlalchemy import text
    from sqlmodel import Session, select
    from tests.modules.ads.conftest import directory_seed
    from tests.modules.ads.test_directory_large_publish import _seed_material_background
    from tests.modules.ads.test_directory_sync import _authority, _page, _run
    from tests.modules.conftest import create_context

    report = {
        "complete": False,
        "slo_qualified": False,
        "real_commit": True,
        "fixture": {
            "background_rows": args.background_rows,
            "objects": args.objects,
            "materials": args.materials,
            "shared_video": args.shared_video,
        },
        "rounds": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(args.output)

    def counts(connection, tenant, version):
        result = {}
        # 固定内部表名，不允许从CLI插入SQL标识符。
        for table in (
            "ad_object",
            "ad_entity_read",
            "ad_material_reference",
            "current_ad_material",
        ):
            identity_column = (
                "remote_id"
                if table in {"ad_object", "ad_entity_read"}
                else "ad_remote_id"
            )
            result[table] = connection.execute(
                text(
                    f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant AND published_version=:version AND {identity_column} LIKE 'large-%'"
                ),
                {"tenant": tenant, "version": version},
            ).scalar_one()
        return result

    try:
        with Session(engine, expire_on_commit=False) as session:
            started = monotonic()
            if args.reuse_fixture:
                from app.core.context import TenantContext
                from app.modules.accounts.models import TikTokConnection
                from app.modules.tenants.models import TenantMembership

                manifest = json.loads(args.reuse_fixture.read_text())
                if (
                    not manifest.get("complete")
                    or manifest["fixture"] != report["fixture"]
                ):
                    raise ValueError("completed fixture and exact sizes required")
                tenant = UUID(manifest["tenant_id"])
                connection = session.exec(
                    select(TikTokConnection).where(TikTokConnection.tenant_id == tenant)
                ).one()
                member = session.exec(
                    select(TenantMembership).where(TenantMembership.tenant_id == tenant)
                ).one()
                context = TenantContext(
                    tenant_id=tenant, actor_id=member.user_id, role="operator"
                )
                seed = SimpleNamespace(
                    context=context,
                    bc_id="bc-own",
                    connection=connection,
                    ref=EntityRef(tenant, "account-a", "ad", "large-0"),
                )
                report["reused_committed_fixture"] = True
            else:
                context, other = create_context(session), create_context(session)
                seed = directory_seed.__wrapped__(session, context, other)
                _authority(session, seed)
                if args.background_rows:
                    _seed_material_background(session, seed, args.background_rows)
            session.commit()
            report["seed_seconds"] = monotonic() - started
            report["tenant_id"] = str(context.tenant_id)
            report["schema_revision"] = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            session.commit()
            save()
            template = _page(seed).items[0]
            items = tuple(
                replace(
                    template,
                    ref=EntityRef(context.tenant_id, "account-a", "ad", f"large-{i}"),
                    configuration={"creative": "x" * 1024},
                )
                for i in range(args.objects)
            )
            materials = tuple(
                AdMaterialUsage(
                    use_ref=MaterialUseRef(
                        items[i % len(items)].ref,
                        "shared-video" if args.shared_video else f"video-{i}",
                        str(i),
                        "VIDEO",
                    ),
                    local_material_id=None,
                    operation_status="ENABLE",
                    complete=True,
                    name=f"material-{i}",
                    creative_ids=(f"creative-{i}",),
                )
                for i in range(args.materials)
            )
            previous = None
            expected = {
                "ad_object": args.objects,
                "ad_entity_read": args.objects,
                "ad_material_reference": args.materials,
                "current_ad_material": args.materials,
            }
            for iteration in range(2):
                run = _run(session, seed, kind="ad")
                stage_directory_page(
                    session,
                    run_id=run.id,
                    page=DirectoryPage(items, materials, None, True, CallEvidence()),
                    claim_generation=1,
                )
                session.commit()  # 与真实链路相同：页先持久化，发布是另一事务。
                started = monotonic()
                version = publish_directory(session, run_id=run.id, claim_generation=1)
                publish_seconds = monotonic() - started
                # 单独验证未提交状态对其他连接不可见；此观察耗时不计发布/提交SLO。
                visibility_started = monotonic()
                with engine.connect() as observer:
                    assert counts(
                        observer, context.tenant_id, version
                    ) == dict.fromkeys(expected, 0)
                    if previous is not None:
                        assert counts(observer, context.tenant_id, previous) == expected
                visibility_seconds = monotonic() - visibility_started
                commit_started = monotonic()
                session.commit()
                commit_seconds = monotonic() - commit_started
                transaction_wall_seconds = monotonic() - started
                with engine.connect() as observer:
                    actual = counts(observer, context.tenant_id, version)
                    assert actual == expected, actual
                    for table in ("ad_material_reference", "current_ad_material"):
                        background = observer.execute(
                            text(
                                f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant AND ad_remote_id LIKE 'background-ad-%'"
                            ),
                            {"tenant": context.tenant_id},
                        ).scalar_one()
                        assert background == args.background_rows
                report["rounds"].append(
                    {
                        "iteration": iteration + 1,
                        "publish_seconds": publish_seconds,
                        "commit_seconds": commit_seconds,
                        "visibility_probe_seconds": visibility_seconds,
                        "transaction_wall_seconds": transaction_wall_seconds,
                        "publish_and_commit_seconds": publish_seconds + commit_seconds,
                        "within_ten_seconds": publish_seconds + commit_seconds <= 10,
                        "row_counts": actual,
                        "uncommitted_hidden": True,
                    }
                )
                save()
                previous = version
        report["complete"] = True
        save()
        print(
            json.dumps(
                {
                    "complete": True,
                    "output": str(args.output),
                    "rounds": report["rounds"],
                }
            )
        )
        return 0
    except Exception as error:  # noqa: BLE001 - 只输出类型，避免数据库连接信息外泄。
        report["error_code"] = type(error).__name__
        save()
        print(
            f"benchmark stopped: {type(error).__name__}; inspect the local artifact",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
