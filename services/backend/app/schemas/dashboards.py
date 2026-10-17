"""Dashboard profiles (spec §05, §12): which spaces a dashboard shows and how its widgets are laid out.

The backend checks the layout's shape and size. Each widget's `config` is owned and validated by
the frontend widget registry, which migrates old versions and falls back to defaults."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.schemas.common import Name

GRID_COLUMNS = 12
MAX_WIDGETS = 40
MAX_CONFIG_BYTES = 16_000

WidgetKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
WidgetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]{1,100}$")]


class WidgetPlacement(BaseModel):
    key: WidgetKey = Field(description="Unique within the dashboard")
    widget: WidgetId = Field(description="Registry id, e.g. agenda.upcoming-events")
    version: int = Field(ge=1, description="Config version the widget wrote")
    config: dict[str, Any] = Field(default_factory=dict)
    x: int = Field(ge=0, lt=GRID_COLUMNS)
    y: int = Field(ge=0, le=1000)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=50)

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError(f"widget extends past column {GRID_COLUMNS}")
        if len(json.dumps(self.config)) > MAX_CONFIG_BYTES:
            raise ValueError(f"config is larger than {MAX_CONFIG_BYTES} bytes")
        return self


Layout = Annotated[list[WidgetPlacement], Field(max_length=MAX_WIDGETS)]


def _unique_keys(layout: list[WidgetPlacement] | None) -> None:
    if layout is not None and len({p.key for p in layout}) != len(layout):
        raise ValueError("widget keys must be unique")


class DashboardCreate(BaseModel):
    name: Name
    space_ids: list[UUID] = Field(min_length=1, max_length=20)
    layout: Layout = Field(default_factory=list)

    @model_validator(mode="after")
    def _keys(self) -> Self:
        _unique_keys(self.layout)
        return self


class DashboardPatch(BaseModel):
    name: Name | None = None
    space_ids: list[UUID] | None = Field(None, min_length=1, max_length=20)
    layout: Layout | None = None
    position: float | None = None

    @model_validator(mode="after")
    def _keys(self) -> Self:
        _unique_keys(self.layout)
        return self


class Dashboard(BaseModel):
    id: UUID
    name: str
    space_ids: list[UUID] = Field(description="Spaces shown; ones you can no longer access are left out")
    layout: list[WidgetPlacement]
    position: float
    revision: int
    created_at: datetime
    updated_at: datetime
