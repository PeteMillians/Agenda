from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import Name


class ListCreate(BaseModel):
    name: Name


class ListPatch(BaseModel):
    name: Name | None = None


class ListItem(BaseModel):
    id: UUID
    list_id: UUID
    text: str
    checked: bool
    position: float
    revision: int
    created_at: datetime
    updated_at: datetime


class ListSummary(BaseModel):
    id: UUID
    space_id: UUID
    name: str
    item_count: int
    checked_count: int
    archived_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ListDetail(ListSummary):
    items: list[ListItem]


class ItemCreate(BaseModel):
    text: Name
    position: float | None = Field(None, description="Omit to append to the end.")


class ItemPatch(BaseModel):
    text: Name | None = None
    checked: bool | None = None
    position: float | None = Field(None, description="Sort key; to move between a and b, send (a+b)/2.")
