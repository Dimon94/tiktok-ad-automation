from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.context import TenantContext
from app.models import User


def create_context(session: Session, *, role: str = "operator") -> TenantContext:
    user = User(
        username=f"{uuid4()}",
        hashed_password="unused",
        is_active=True,
        is_superuser=False,
    )
    tenant_id = uuid4()
    # 不把今日 ORM 的新增列 flush 到旧迁移 schema。
    from sqlalchemy import MetaData, Table

    for table_name, values in [
        (
            "user",
            {
                "id": user.id,
                "username": user.username,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
                "created_at": user.created_at,
                "hashed_password": user.hashed_password,
            },
        ),
        ("tenant", {"id": tenant_id, "name": f"test-{uuid4()}", "active": True}),
        (
            "tenant_membership",
            {"tenant_id": tenant_id, "user_id": user.id, "role": role, "active": True},
        ),
    ]:
        table = Table(table_name, MetaData(), autoload_with=session.connection())
        session.execute(
            table.insert().values(
                **{key: value for key, value in values.items() if key in table.c}
            )
        )
    session.flush()
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role=role)


@pytest.fixture
def context(session: Session) -> TenantContext:
    return create_context(session)


@pytest.fixture
def other_context(session: Session) -> TenantContext:
    return create_context(session)
