from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import recurrence as r

NY = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestNormalize:
    def test_canonical_form(self):
        assert r.normalize("rrule:byday=tu;freq=weekly", NY) == "FREQ=WEEKLY;BYDAY=TU"

    @pytest.mark.parametrize("rule", ["FREQ=HOURLY", "FREQ=MINUTELY", "BYDAY=TU", "FREQ=WEEKLY;BYDAY"])
    def test_rejects_unsupported_or_malformed(self, rule):
        with pytest.raises(r.InvalidRule):
            r.normalize(rule, NY)

    def test_rejects_count_and_until(self):
        with pytest.raises(r.InvalidRule):
            r.normalize("FREQ=DAILY;COUNT=3;UNTIL=20261231T000000", NY)

    def test_rejects_huge_count(self):
        with pytest.raises(r.InvalidRule):
            r.normalize("FREQ=DAILY;COUNT=5000", NY)

    def test_utc_until_becomes_local_wall_time(self):
        # 2026-12-31 05:00Z is 00:00 in New York (EST, UTC-5)
        assert r.normalize("FREQ=DAILY;UNTIL=20261231T050000Z", NY) == "FREQ=DAILY;UNTIL=20261231T000000"


class TestExpandTimed:
    def test_wall_clock_survives_dst_end(self):
        # Tuesdays 09:00 New York; DST ends Sunday 2026-11-01
        start = utc(2026, 10, 20, 13)  # 09:00 EDT
        starts = r.expand_timed("FREQ=WEEKLY;BYDAY=TU", start, NY, HOUR, utc(2026, 10, 19), utc(2026, 11, 11))
        assert starts == [
            utc(2026, 10, 20, 13),
            utc(2026, 10, 27, 13),
            utc(2026, 11, 3, 14),
            utc(2026, 11, 10, 14),
        ]
        assert {s.astimezone(NY).hour for s in starts} == {9}

    def test_window_overlap_includes_event_already_running(self):
        start = utc(2026, 10, 20, 13)
        # window starts 30 minutes into the 1-hour occurrence
        starts = r.expand_timed(
            "FREQ=DAILY", start, NY, HOUR, utc(2026, 10, 21, 13, 30), utc(2026, 10, 21, 14)
        )
        assert starts == [utc(2026, 10, 21, 13)]

    def test_count_limits_series(self):
        starts = r.expand_timed(
            "FREQ=DAILY;COUNT=3", utc(2026, 1, 1, 15), NY, HOUR, utc(2026, 1, 1), utc(2026, 2, 1)
        )
        assert len(starts) == 3

    def test_local_until_is_inclusive(self):
        rule = "FREQ=DAILY;UNTIL=20260103T100000"
        starts = r.expand_timed(rule, utc(2026, 1, 1, 15), NY, HOUR, utc(2026, 1, 1), utc(2026, 2, 1))
        assert [s.day for s in starts] == [1, 2, 3]


class TestExpandDates:
    def test_multi_day_event_overlapping_window_start(self):
        # 3-day all-day event every week starting Saturday 2026-10-17
        days = r.expand_dates("FREQ=WEEKLY", date(2026, 10, 17), 3, date(2026, 10, 18), date(2026, 10, 25))
        assert days == [date(2026, 10, 17), date(2026, 10, 24)]

    def test_monthly_on_31st_skips_short_months(self):
        days = r.expand_dates("FREQ=MONTHLY", date(2026, 1, 31), 1, date(2026, 1, 1), date(2026, 6, 1))
        assert days == [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)]


class TestOccurrenceMembership:
    def test_timed(self):
        start = utc(2026, 10, 20, 13)
        assert r.is_timed_occurrence("FREQ=WEEKLY", start, NY, utc(2026, 11, 3, 14))  # after DST end
        assert not r.is_timed_occurrence("FREQ=WEEKLY", start, NY, utc(2026, 11, 3, 13))

    def test_dates(self):
        assert r.is_date_occurrence("FREQ=WEEKLY;BYDAY=MO,FR", date(2026, 10, 19), date(2026, 10, 23))
        assert not r.is_date_occurrence("FREQ=WEEKLY;BYDAY=MO,FR", date(2026, 10, 19), date(2026, 10, 22))


class TestSplit:
    def test_open_ended_split_uses_until(self):
        start = utc(2026, 10, 20, 13)
        head, tail = r.split_timed("FREQ=WEEKLY", start, NY, utc(2026, 11, 3, 14))
        assert head == "FREQ=WEEKLY;UNTIL=20261103T085959"
        assert tail == "FREQ=WEEKLY"
        before = r.expand_timed(head, start, NY, HOUR, utc(2026, 10, 1), utc(2027, 1, 1))
        assert before == [utc(2026, 10, 20, 13), utc(2026, 10, 27, 13)]

    def test_count_is_divided(self):
        head, tail = r.split_dates("FREQ=DAILY;COUNT=10", date(2026, 1, 1), date(2026, 1, 4))
        assert (head, tail) == ("FREQ=DAILY;COUNT=3", "FREQ=DAILY;COUNT=7")

    def test_existing_until_stays_on_tail(self):
        head, tail = r.split_dates("FREQ=DAILY;UNTIL=20260131", date(2026, 1, 1), date(2026, 1, 10))
        assert head == "FREQ=DAILY;UNTIL=20260109"
        assert tail == "FREQ=DAILY;UNTIL=20260131"


class TestChoreSchedule:
    RULE = "FREQ=WEEKLY;BYDAY=TU"
    DUE = date(2026, 10, 20)  # a Tuesday

    @pytest.mark.parametrize(
        ("completed", "expected"),
        [
            (date(2026, 10, 19), date(2026, 10, 27)),  # early: still the next Tuesday after due
            (date(2026, 10, 20), date(2026, 10, 27)),  # on time
            (date(2026, 10, 29), date(2026, 11, 3)),  # late: next Tuesday, no backlog
            (date(2026, 11, 12), date(2026, 11, 17)),  # very late: skips missed weeks
        ],
    )
    def test_next_due(self, completed, expected):
        assert r.next_date(self.RULE, self.DUE, max(self.DUE, completed)) == expected

    def test_exhausted_rule_has_no_next(self):
        assert r.next_date("FREQ=DAILY;COUNT=2", date(2026, 1, 1), date(2026, 1, 2)) is None

    def test_timed_next_keeps_local_time(self):
        anchor = utc(2026, 10, 27, 13)  # Tue 09:00 EDT
        nxt = r.next_datetime(self.RULE, anchor, NY, anchor)
        assert nxt == utc(2026, 11, 3, 14)  # Tue 09:00 EST


class TestOffPatternStart:
    """Imported series can start off-pattern; RFC 5545 still counts DTSTART as an occurrence."""

    def test_dtstart_is_included(self):
        # starts Monday 2026-10-19, rule says Tuesdays
        days = r.expand_dates(
            "FREQ=WEEKLY;BYDAY=TU", date(2026, 10, 19), 1, date(2026, 10, 1), date(2026, 11, 1)
        )
        assert days == [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 27)]

    def test_dtstart_is_a_member(self):
        assert r.is_date_occurrence("FREQ=WEEKLY;BYDAY=TU", date(2026, 10, 19), date(2026, 10, 19))
