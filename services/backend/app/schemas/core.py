"""Setup, profile, spaces, and people."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import PALETTE, Color, Name, TimeZoneName

SpaceType = Literal["personal", "work", "family", "custom"]


class SetupStatus(BaseModel):
    completed: bool


class SpaceCreate(BaseModel):
    name: Name
    type: SpaceType = "custom"
    color: Color = PALETTE[0]


DEFAULT_SPACES = [
    SpaceCreate(name="Personal", type="personal", color=PALETTE[0]),
    SpaceCreate(name="Work", type="work", color=PALETTE[4]),
    SpaceCreate(name="Family", type="family", color=PALETTE[1]),
]


class SetupRequest(BaseModel):
    display_name: Name
    timezone: TimeZoneName
    spaces: list[SpaceCreate] = Field(
        default_factory=lambda: list(DEFAULT_SPACES), min_length=1, max_length=20
    )


class User(BaseModel):
    id: UUID
    display_name: str
    timezone: str
    created_at: datetime


class UserPatch(BaseModel):
    display_name: Name | None = None
    timezone: TimeZoneName | None = None


class Space(BaseModel):
    id: UUID
    name: str
    type: SpaceType
    color: str
    role: Literal["owner", "editor", "viewer"]
    archived_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class SpacePatch(BaseModel):
    name: Name | None = None
    type: SpaceType | None = None
    color: Color | None = None
    archived: bool | None = None


class Person(BaseModel):
    id: UUID
    space_id: UUID
    display_name: str
    color: str
    revision: int
    created_at: datetime
    updated_at: datetime


class PersonCreate(BaseModel):
    display_name: Name
    color: Color = PALETTE[2]


class PersonPatch(BaseModel):
    display_name: Name | None = None
    color: Color | None = None
