"""Recurrence translation between Agenda's RRULEs and each provider's own form."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.application.events import Timing
from app.integrations.errors import Unrepresentable
from app.integrations.google import mapping as google
from app.integrations.microsoft import mapping as microsoft
from app.integrations.model import Outgoing

NY = "America/New_York"
TUESDAY_9AM = Timing(
    False, start_at=datetime(2026, 10, 20, 13, tzinfo=UTC), end_at=datetime(2026, 10, 20, 13, 30, tzinfo=UTC)
)
ALL_DAY = Timing(True, start_date=date(2026, 10, 24), end_date=date(2026, 10, 25))


def outgoing(rule: str, timing: Timing = TUESDAY_9AM, exdates: tuple[str, ...] = ()) -> Outgoing:
    return Outgoing("Standup", "", "", NY, timing, rule, exdates)


class TestGoogle:
    def test_until_goes_out_in_utc_and_exdates_in_local_time(self):
        lines = google.recurrence_lines(
            outgoing(
                "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261109T235959", exdates=("2026-11-03T14:00:00.000000+00:00",)
            )
        )
        assert lines == [
            "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261110T045959Z",
            "EXDATE;TZID=America/New_York:20261103T090000",
        ]

    def test_all_day_exdates_are_dates(self):
        lines = google.recurrence_lines(outgoing("FREQ=YEARLY", ALL_DAY, ("2027-10-24",)))
        assert lines == ["RRULE:FREQ=YEARLY", "EXDATE;VALUE=DATE:20271024"]

    def test_instance_ids_follow_googles_naming(self):
        assert google.instance_id("abc", "2026-10-27T13:00:00.000000+00:00", False) == "abc_20261027T130000Z"
        assert google.instance_id("abc", "2026-10-24", True) == "abc_20261024"

    def test_patch_body_never_touches_fields_agenda_does_not_own(self):
        body = google.body(outgoing("FREQ=DAILY"))
        assert set(body) == {"summary", "description", "location", "start", "end", "recurrence"}


class TestMicrosoft:
    @pytest.mark.parametrize(
        ("rule", "pattern"),
        [
            ("FREQ=DAILY;INTERVAL=2", {"type": "daily", "interval": 2}),
            (
                "FREQ=WEEKLY;BYDAY=TU,TH",
                {
                    "type": "weekly",
                    "interval": 1,
                    "daysOfWeek": ["tuesday", "thursday"],
                    "firstDayOfWeek": "monday",
                },
            ),
            ("FREQ=MONTHLY", {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 20}),
            (
                "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=3",
                {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["tuesday"], "index": "third"},
            ),
            (
                "FREQ=MONTHLY;BYDAY=-1TU",
                {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["tuesday"], "index": "last"},
            ),
            ("FREQ=YEARLY", {"type": "absoluteYearly", "interval": 1, "month": 10, "dayOfMonth": 20}),
        ],
    )
    def test_rules_become_patterns(self, rule, pattern):
        assert microsoft.rrule_to_pattern(rule, TUESDAY_9AM, NY)["pattern"] == pattern

    def test_ranges(self):
        span = microsoft.rrule_to_pattern("FREQ=WEEKLY;COUNT=4", TUESDAY_9AM, NY)["range"]
        assert span == {
            "type": "numbered",
            "numberOfOccurrences": 4,
            "startDate": "2026-10-20",
            "recurrenceTimeZone": NY,
        }
        # a split ends the series one second before an occurrence: that day is not included
        split = microsoft.rrule_to_pattern("FREQ=WEEKLY;UNTIL=20261110T085959", TUESDAY_9AM, NY)["range"]
        assert split["endDate"] == "2026-11-09"
        assert microsoft.rrule_to_pattern("FREQ=WEEKLY;UNTIL=20261110T235959", TUESDAY_9AM, NY)["range"][
            "endDate"
        ] == ("2026-11-10")

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=MONTHLY;BYMONTHDAY=1,15",
            "FREQ=WEEKLY;BYHOUR=9",
            "FREQ=DAILY;BYDAY=MO",
            "FREQ=MONTHLY;BYDAY=1MO,2TU",
        ],
    )
    def test_what_outlook_cannot_store(self, rule):
        with pytest.raises(Unrepresentable):
            microsoft.rrule_to_pattern(rule, TUESDAY_9AM, NY)

    def test_patterns_become_rules(self):
        rec = {
            "pattern": {
                "type": "relativeYearly",
                "interval": 1,
                "month": 11,
                "daysOfWeek": ["thursday"],
                "index": "fourth",
            },
            "range": {"type": "endDate", "startDate": "2026-11-26", "endDate": "2030-12-31"},
        }
        assert (
            microsoft.pattern_to_rrule(rec, all_day=True)
            == "FREQ=YEARLY;BYMONTH=11;BYDAY=TH;BYSETPOS=4;UNTIL=20301231"
        )

    def test_windows_zone_names(self):
        assert microsoft.zone("Eastern Standard Time") == NY
        assert microsoft.zone("Europe/Paris") == "Europe/Paris"
        assert microsoft.zone("tzone://Microsoft/Custom", "UTC") == "UTC"

    def test_all_day_occurrence_keys(self):
        assert microsoft.occurrence_key("2026-10-24T00:00:00Z", True, NY) == "2026-10-24"
        assert microsoft.occurrence_key("2026-10-24T04:00:00.0000000Z", True, NY) == "2026-10-24"
        assert (
            microsoft.occurrence_key("2026-10-27T13:00:00Z", False, NY) == "2026-10-27T13:00:00.000000+00:00"
        )
