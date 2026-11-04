from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.db import engine
from app.models import User
from app.modules.accounts.models import TenantBC
from app.modules.business.models import BusinessPublication, BusinessRun, BusinessScope
from app.modules.business.projection import advance_run
from app.modules.business.source import source_snapshot
from app.modules.tenants.models import Tenant, TenantMembership


def test_real_postgres_bc_lock_and_lost_worker_recovery():
    tenant_id, actor_id = uuid4(), uuid4()
    bc_id = f"business-concurrency-{uuid4().hex}"
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    try:
        with Session(engine) as setup:
            setup.add_all(
                [
                    Tenant(id=tenant_id, name="business-lock-test"),
                    User(
                        id=actor_id,
                        username=f"business-{actor_id.hex[:12]}",
                        hashed_password="test-only",
                        is_active=True,
                    ),
                ]
            )
            setup.flush()
            setup.add_all(
                [
                    TenantBC(tenant_id=tenant_id, bc_id=bc_id),
                    TenantMembership(
                        tenant_id=tenant_id,
                        user_id=actor_id,
                        role="tenant_admin",
                        active=True,
                    ),
                ]
            )
            setup.flush()
            scope = BusinessScope(
                tenant_id=tenant_id,
                bc_id=bc_id,
                actor_id=actor_id,
                start_date=date(2026, 10, 1),
            )
            setup.add(scope)
            setup.flush()
            inputs, signature = source_snapshot(
                setup, scope, date(2026, 10, 1), date(2026, 10, 1), now
            )
            run = BusinessRun(
                tenant_id=tenant_id,
                bc_id=bc_id,
                actor_id=actor_id,
                quarter=date(2026, 10, 1),
                end_date=date(2026, 10, 1),
                cursor=date(2026, 10, 1),
                scope_revision=scope.revision,
                source_signature=signature,
                inputs={**inputs, "dirty_start": "2026-10-01", "after": None},
            )
            setup.add(run)
            setup.commit()
            run_id = run.id
        with Session(engine) as owner, Session(engine) as competitor:
            owner.exec(
                select(TenantBC)
                .where(TenantBC.tenant_id == tenant_id, TenantBC.bc_id == bc_id)
                .with_for_update()
            ).one()
            assert not advance_run(competitor, run_id, now=now)
            competitor.commit()
            assert competitor.get(BusinessRun, run_id).cursor == date(2026, 10, 1)
            # 原worker事务退出后，同一持久任务可由下一消费者继续。
            owner.rollback()
        for _ in range(6):
            with Session(engine) as recovery:
                done = advance_run(recovery, run_id, now=now)
                recovery.commit()
                if done:
                    break
        with Session(engine) as verify:
            assert verify.get(BusinessRun, run_id).status == "COMPLETE"
            assert advance_run(verify, run_id, now=now)
            assert (
                len(
                    verify.exec(
                        select(BusinessPublication).where(
                            BusinessPublication.tenant_id == tenant_id
                        )
                    ).all()
                )
                == 1
            )
    finally:
        with Session(engine) as cleanup:
            for model in (
                BusinessPublication,
                BusinessRun,
                BusinessScope,
                TenantMembership,
                TenantBC,
                Tenant,
            ):
                cleanup.execute(
                    delete(model).where(model.tenant_id == tenant_id)
                    if model is not Tenant
                    else delete(Tenant).where(Tenant.id == tenant_id)
                )
            cleanup.execute(delete(User).where(User.id == actor_id))
            cleanup.commit()
