"""Events, recurring series, and occurrences (ADR 0002)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, StringConstraints, model_validator

from app.schemas.common import LongText, Name, TimeZoneName

Scope = Literal["this", "following", "all"]
Location = Annotated[str, StringConstraints(max_length=500)]


class EventCreate(BaseModel):
    title: Name
    description: LongText = ""
    location: Location = ""
    all_day: bool = False
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    start_date: date | None = None
    end_date: date | None = None  # exclusive: a one-day event ends the next day
    timezone: TimeZoneName | None = None  # defaults to the owner's time zone
    rrule: str | None = None

    @model_validator(mode="after")
    def _timing(self) -> Self:
        if self.all_day:
            if not (self.start_date and self.end_date) or self.start_at or self.end_at:
                raise ValueError("all-day events need start_date and end_date only")
        elif not (self.start_at and self.end_at) or self.start_date or self.end_date:
            raise ValueError("timed events need start_at and end_at only")
        return self


class EventPatch(BaseModel):
    """Only fields present change. Timing fields are for the occurrence being edited; with
    scope=all, the series moves by the same amount."""

    title: Name | None = None
    description: LongText | None = None
    location: Location | None = None
    all_day: bool | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: TimeZoneName | None = None
    rrule: str | None = None  # null with scope=all turns a series into a single event


class Event(BaseModel):
    """A stored row: single event, series master, or exception."""

    id: UUID
    space_id: UUID
    title: str
    description: str
    location: str
    all_day: bool
    start_at: datetime | None
    end_at: datetime | None
    start_date: date | None
    end_date: date | None
    timezone: str
    rrule: str | None
    series_id: UUID | None
    original_start: str | None
    status: Literal["confirmed", "cancelled"]
    revision: int
    created_at: datetime
    updated_at: datetime


class Occurrence(BaseModel):
    """One appearance on the calendar. Edit it through `event_id` (+ `occurrence_start` and a
    scope when `is_recurring`), sending If-Match: `revision`."""

    event_id: UUID
    occurrence_start: str | None
    is_recurring: bool
    is_exception: bool
    revision: int
    title: str
    description: str
    location: str
    all_day: bool
    start_at: datetime | None
    end_at: datetime | None
    start_date: date | None
    end_date: date | None
    timezone: str
    rrule: str | None
