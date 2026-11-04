import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import field_validator
from sqlalchemy import CheckConstraint, DateTime
from sqlmodel import Field, Relationship, SQLModel
from sqlmodel._compat import SQLModelConfig

from app.core.usernames import Username


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


# Shared properties
class UserBase(SQLModel):
    username: Username = Field(unique=True, index=True, max_length=64)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on creation
class UserCreate(UserBase):
    model_config = SQLModelConfig(extra="forbid")
    password: str = Field(min_length=8, max_length=128)


# Properties to receive via API on update, all are optional
class UserUpdate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    @field_validator("username", "password", "is_active", "is_superuser")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    username: Username | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    is_superuser: bool | None = None
    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    @field_validator("username")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Username cannot be null")
        return value

    full_name: str | None = Field(default=None, max_length=255)
    username: Username | None = Field(default=None, max_length=64)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    __table_args__ = (
        CheckConstraint(
            "username ~ '^[a-z0-9_.-]{3,64}$'", name="ck_user_username_format"
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    items: list[Item] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    created_at: datetime | None = None


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


# 广告管理的复合外键及任务表在普通应用进程也必须登记，不能只依赖迁移导入。
from app.modules.ad_management import (  # noqa: E402
    batch_models as management_batch_models,  # noqa: E402, F401
)
from app.modules.ad_management import models as ad_management_models  # noqa: E402, F401
from app.modules.builds import (  # noqa: E402
    carousel_music_models,  # noqa: E402,F401
    image_composition_models,  # noqa: E402, F401
)
from app.modules.builds.image_composition_state import (  # noqa: E402,F401
    ImageCandidateExclusion,
    ImageCandidateItem,
    ImageCandidateSnapshot,
    ImageCompositionPlan,
    ImageCompositionPlanUnit,
    ImageCompositionRequest,
    ImageCompositionRun,
    ImageCompositionState,
)
from app.modules.business import models as business_models  # noqa: E402, F401
from app.modules.materials import reissue_models  # noqa: E402, F401
from app.modules.materials.carousel_models import (  # noqa: E402, F401
    CarouselPresetItem,
    CarouselPresetVersion,
)
from app.modules.reporting import store_models  # noqa: E402, F401
from app.modules.reporting.read_models import (  # noqa: E402
    models as reporting_read_models,  # noqa: E402, F401
)
from app.modules.sync_control import models as sync_control_models  # noqa: E402, F401
