"""Tasks and repeating chores (ADR 0002)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.schemas.common import LongText, Name

TaskStatus = Literal["open", "done", "archived"]


class Repeat(BaseModel):
    """How a chore comes back after it is completed."""

    mode: Literal["none", "schedule", "after_completion"] = "none"
    rrule: str | None = Field(None, description="mode=schedule: RRULE, e.g. FREQ=WEEKLY;BYDAY=TU")
    interval_days: int | None = Field(None, ge=1, le=3650, description="mode=after_completion")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.mode == "none" and (self.rrule or self.interval_days):
            raise ValueError("mode 'none' takes no rrule or interval_days")
        if self.mode == "schedule" and (not self.rrule or self.interval_days):
            raise ValueError("mode 'schedule' needs rrule and no interval_days")
        if self.mode == "after_completion" and (not self.interval_days or self.rrule):
            raise ValueError("mode 'after_completion' needs interval_days and no rrule")
        return self


class TaskCreate(BaseModel):
    title: Name
    notes: LongText = ""
    due_date: date | None = None
    due_at: AwareDatetime | None = None
    assignee_person_id: UUID | None = None
    repeat: Repeat = Field(default_factory=Repeat)

    @model_validator(mode="after")
    def _one_due(self) -> Self:
        if self.due_date and self.due_at:
            raise ValueError("set due_date or due_at, not both")
        return self


class TaskPatch(BaseModel):
    """Only fields present in the request change; send null to clear a nullable field."""

    title: Name | None = None
    notes: LongText | None = None
    due_date: date | None = None
    due_at: AwareDatetime | None = None
    assignee_person_id: UUID | None = None
    repeat: Repeat | None = None
    status: Literal["open", "done"] | None = None


class Task(BaseModel):
    id: UUID
    space_id: UUID
    title: str
    notes: str
    due_date: date | None
    due_at: datetime | None
    assignee_person_id: UUID | None
    repeat: Repeat
    chain_id: UUID
    previous_task_id: UUID | None
    status: TaskStatus
    completed_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
