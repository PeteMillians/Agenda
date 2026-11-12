"""One vocabulary for the contract suite (spec §16): each harness drives its provider's fake the
way a person would in that provider's own app, so tests/test_calendar_contract.py runs the same
scenarios against Google and Microsoft. Times are New York wall-clock, "2026-10-21T14:30"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from tests.conftest import AUTH, PORT
from tests.fake_google import FakeGoogle
from tests.fake_microsoft import FakeMicrosoft

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Remote:
    title: str
    start: str  # "MM-DD HH:MM" in New York, or "YYYY-MM-DD" for all-day
    end: str


def _ny(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NY)


def _label(moment: datetime) -> str:
    return moment.astimezone(NY).strftime("%m-%d %H:%M")


class Harness:
    provider: str
    client_config: dict[str, str]
    calendar = "family-cal"
    fake: Any

    def connect(self, client: Any, *, write: bool = False) -> dict:
        start = client.post(
            f"/api/v1/integrations/{self.provider}/connect/start?write={str(write).lower()}", headers=AUTH
        )
        assert start.status_code == 200, start.text
        url = start.json()["data"]["authorization_url"]
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        code = self.fake.authorize(url)
        page = client.get(f"{self.callback_base}/callback?state={query['state']}&code={code}")
        assert page.status_code == 200, page.text
        accounts = client.get("/api/v1/external-accounts", headers=AUTH).json()["data"]
        return next(a for a in accounts if a["provider"] == self.provider)

    @property
    def callback_base(self) -> str:
        host = "localhost" if self.provider == "microsoft" else "127.0.0.1"
        return f"http://{host}:{PORT}/api/v1/integrations/{self.provider}"

    @property
    def offline(self) -> bool:
        return self.fake.offline

    @offline.setter
    def offline(self, value: bool) -> None:
        self.fake.offline = value


class GoogleHarness(Harness):
    provider = "google"
    client_config = {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-test"}

    def __init__(self) -> None:
        self.fake = FakeGoogle()
        self.fake.add_calendar("primary-cal", "Pete", primary=True)
        self.fake.add_calendar("family-cal", "Family shared")

    @staticmethod
    def _moment(local: str) -> dict[str, str]:
        return {"dateTime": _ny(local).isoformat(), "timeZone": "America/New_York"}

    def single(self, event_id: str, title: str, start: str, end: str) -> None:
        self.fake.put(
            self.calendar,
            {"id": event_id, "summary": title, "start": self._moment(start), "end": self._moment(end)},
        )

    def all_day(self, event_id: str, title: str, start: str, end: str) -> None:
        self.fake.put(
            self.calendar, {"id": event_id, "summary": title, "start": {"date": start}, "end": {"date": end}}
        )

    def weekly(
        self, event_id: str, title: str, start: str, end: str, *, exdates: tuple[str, ...] = ()
    ) -> None:
        recurrence = [f"RRULE:FREQ=WEEKLY;BYDAY={_ny(start).strftime('%a').upper()[:2]}"]
        if exdates:
            values = ",".join(_ny(x).strftime("%Y%m%dT%H%M%S") for x in exdates)
            recurrence.append(f"EXDATE;TZID=America/New_York:{values}")
        self.fake.put(
            self.calendar,
            {
                "id": event_id,
                "summary": title,
                "start": self._moment(start),
                "end": self._moment(end),
                "recurrence": recurrence,
            },
        )

    def occurrence(self, master: str, original: str) -> str:
        return f"{master}_{_ny(original).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    def rename(self, event_id: str, title: str) -> None:
        self.fake.edit(self.calendar, event_id, summary=title)

    def move(self, event_id: str, start: str, end: str) -> None:
        self.fake.edit(self.calendar, event_id, start=self._moment(start), end=self._moment(end))

    def remove(self, event_id: str) -> None:
        self.fake.delete(self.calendar, event_id)

    def remote(self, event_id: str) -> Remote | None:
        item = self.fake.get(self.calendar, event_id)
        if item is None or item.get("status") == "cancelled":
            return None
        if "date" in item["start"]:
            return Remote(item["summary"], item["start"]["date"], item["end"]["date"])
        return Remote(
            item["summary"],
            _label(datetime.fromisoformat(item["start"]["dateTime"])),
            _label(datetime.fromisoformat(item["end"]["dateTime"])),
        )

    def ids_titled(self, title: str) -> list[str]:
        return [
            e["id"]
            for e in self.fake.calendars[self.calendar].events.values()
            if e.get("summary") == title and e.get("status") != "cancelled" and "recurringEventId" not in e
        ]

    def series_ends(self, event_id: str) -> date | None:
        """Last day the series can have an occurrence (from UNTIL), or None if open-ended."""
        rule = next(
            line for line in self.fake.get(self.calendar, event_id)["recurrence"] if line.startswith("RRULE")
        )
        parts = dict(p.split("=") for p in rule.removeprefix("RRULE:").split(";"))
        if "UNTIL" not in parts:
            return None
        return datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).astimezone(NY).date()

    def on_write(self, hook: Any) -> None:
        self.fake.before_write = lambda cal_id, event_id: hook(event_id)


class MicrosoftHarness(Harness):
    provider = "microsoft"
    client_config = {"client_id": "00000000-1111-2222-3333-444444444444"}
    zone = "Eastern Standard Time"  # Outlook's own name for New York's zone

    def __init__(self) -> None:
        self.fake = FakeMicrosoft()
        self.fake.add_calendar("primary-cal", "Calendar", default=True)
        self.fake.add_calendar("family-cal", "Family shared")

    def _moment(self, local: str) -> dict[str, str]:
        return {"dateTime": datetime.fromisoformat(local).isoformat(), "timeZone": self.zone}

    def single(self, event_id: str, title: str, start: str, end: str) -> None:
        self.fake.put(
            self.calendar,
            {"id": event_id, "subject": title, "start": self._moment(start), "end": self._moment(end)},
        )

    def all_day(self, event_id: str, title: str, start: str, end: str) -> None:
        self.fake.put(
            self.calendar,
            {
                "id": event_id,
                "subject": title,
                "isAllDay": True,
                "start": self._moment(f"{start}T00:00"),
                "end": self._moment(f"{end}T00:00"),
            },
        )

    def weekly(
        self, event_id: str, title: str, start: str, end: str, *, exdates: tuple[str, ...] = ()
    ) -> None:
        first = _ny(start)
        self.fake.put(
            self.calendar,
            {
                "id": event_id,
                "subject": title,
                "start": self._moment(start),
                "end": self._moment(end),
                "recurrence": {
                    "pattern": {
                        "type": "weekly",
                        "interval": 1,
                        "daysOfWeek": [first.strftime("%A").lower()],
                        "firstDayOfWeek": "sunday",
                    },
                    "range": {
                        "type": "noEnd",
                        "startDate": first.date().isoformat(),
                        "recurrenceTimeZone": self.zone,
                    },
                },
            },
        )
        for original in exdates:
            self.fake.delete(self.calendar, self.occurrence(event_id, original))

    def occurrence(self, master: str, original: str) -> str:
        return self.fake.occurrence_id(master, _ny(original).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def rename(self, event_id: str, title: str) -> None:
        self.fake.edit(self.calendar, event_id, subject=title)

    def move(self, event_id: str, start: str, end: str) -> None:
        self.fake.edit(self.calendar, event_id, start=self._moment(start), end=self._moment(end))

    def remove(self, event_id: str) -> None:
        self.fake.delete(self.calendar, event_id)

    def remote(self, event_id: str) -> Remote | None:
        item = self.fake.get(self.calendar, event_id)
        if item is None:
            return None
        start = datetime.fromisoformat(item["start"]["dateTime"].split(".")[0]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(item["end"]["dateTime"].split(".")[0]).replace(tzinfo=UTC)
        if item.get("isAllDay"):
            return Remote(item["subject"], start.date().isoformat(), end.date().isoformat())
        return Remote(item["subject"], _label(start), _label(end))

    def ids_titled(self, title: str) -> list[str]:
        return [
            e["id"] for e in self.fake.calendars[self.calendar].events.values() if e.get("subject") == title
        ]

    def series_ends(self, event_id: str) -> date | None:
        span = self.fake.calendars[self.calendar].events[event_id]["recurrence"]["range"]
        return date.fromisoformat(span["endDate"]) if span["type"] == "endDate" else None

    def on_write(self, hook: Any) -> None:
        self.fake.before_write = lambda cal_id, event_id: hook(event_id)


WINDOW = (date(2026, 4, 1), date(2027, 12, 1))  # pins Microsoft's instance window for the dates used


def make(provider: str) -> Harness:
    return GoogleHarness() if provider == "google" else MicrosoftHarness()


def later(local: str, **delta: float) -> str:
    return (datetime.fromisoformat(local) + timedelta(**delta)).isoformat(timespec="minutes")
