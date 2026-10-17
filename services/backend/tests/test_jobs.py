"""Durable job queue and outbox worker: crash, retry, and duplicate-delivery tests (spec §16)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.db import make_engine, migrate
from app.jobs import queue
from app.jobs.queue import JobError
from app.jobs.worker import Worker
from tests.conftest import AUTH

LEASE = timedelta(minutes=1)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "agenda.db"
    migrate(path, tmp_path / "backups")
    return path


def run(coro):
    return asyncio.run(coro)


async def _with_engine(db, fn):
    engine = make_engine(db)
    try:
        return await fn(engine)
    finally:
        await engine.dispose()


async def _enqueue(engine, kind="test.kind", **kwargs):
    async with engine.begin() as conn:
        return await queue.enqueue(conn, kind, {"n": 1}, **kwargs)


def state_of(db, job_id):
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT state, attempts, last_error_category FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return row


def test_enqueue_claim_succeed(db):
    async def scenario(engine):
        job_id = await _enqueue(engine)
        job = await queue.claim(engine, LEASE)
        assert (job.id, job.payload, job.attempts) == (job_id, {"n": 1}, 1)
        assert await queue.claim(engine, LEASE) is None  # leased jobs are not handed out twice
        await queue.succeed(engine, job)
        return job_id

    job_id = run(_with_engine(db, scenario))
    assert state_of(db, job_id) == ("succeeded", 1, None)


def test_idempotency_key_deduplicates(db):
    async def scenario(engine):
        first = await _enqueue(engine, idempotency_key="sync:abc")
        second = await _enqueue(engine, idempotency_key="sync:abc")
        other_kind = await _enqueue(engine, kind="other.kind", idempotency_key="sync:abc")
        return first, second, other_kind

    first, second, other_kind = run(_with_engine(db, scenario))
    assert first == second
    assert other_kind != first


def test_retryable_failure_backs_off(db):
    async def scenario(engine):
        await _enqueue(engine)
        job = await queue.claim(engine, LEASE)
        state = await queue.fail(engine, job, JobError("provider_unavailable", retryable=True))
        not_yet = await queue.claim(engine, LEASE)
        return job.id, state, not_yet

    job_id, state, not_yet = run(_with_engine(db, scenario))
    assert state == "queued"
    assert not_yet is None  # still inside the backoff window
    assert state_of(db, job_id) == ("queued", 1, "provider_unavailable")


def test_retry_after_is_honored(db):
    async def scenario(engine):
        await _enqueue(engine)
        job = await queue.claim(engine, LEASE)
        await queue.fail(
            engine, job, JobError("rate_limited", retryable=True, retry_after=timedelta(hours=1))
        )
        return job.id

    job_id = run(_with_engine(db, scenario))
    with sqlite3.connect(db) as conn:
        available, updated = conn.execute(
            "SELECT available_at, updated_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert datetime.fromisoformat(available) - datetime.fromisoformat(updated) >= timedelta(minutes=59)


def test_dead_letter_after_max_attempts(db):
    async def scenario(engine):
        await _enqueue(engine, max_attempts=2)
        states = []
        for _ in range(2):
            job = await queue.claim(engine, LEASE)
            async with engine.begin() as conn:  # skip the backoff wait
                await conn.exec_driver_sql("UPDATE jobs SET available_at = '2000-01-01T00:00:00+00:00'")
            states.append(await queue.fail(engine, job, JobError("boom", retryable=True)))
            async with engine.begin() as conn:
                await conn.exec_driver_sql("UPDATE jobs SET available_at = '2000-01-01T00:00:00+00:00'")
        return states, await queue.claim(engine, LEASE)

    states, leftover = run(_with_engine(db, scenario))
    assert states == ["queued", "dead_letter"]
    assert leftover is None


def test_permanent_failure_dead_letters_immediately(db):
    async def scenario(engine):
        await _enqueue(engine)
        job = await queue.claim(engine, LEASE)
        return await queue.fail(engine, job, JobError("invalid_grant", retryable=False))

    assert run(_with_engine(db, scenario)) == "dead_letter"


def test_expired_lease_is_reclaimed_after_crash(db):
    """A process that dies mid-job leaves a leased row; its lease expiring makes it claimable."""

    async def scenario(engine):
        await _enqueue(engine)
        crashed = await queue.claim(engine, timedelta(seconds=-1))  # lease already expired
        reclaimed = await queue.claim(engine, LEASE)
        return crashed, reclaimed

    crashed, reclaimed = run(_with_engine(db, scenario))
    assert reclaimed.id == crashed.id
    assert reclaimed.attempts == 2


def test_release_gives_back_the_attempt(db):
    async def scenario(engine):
        await _enqueue(engine)
        job = await queue.claim(engine, LEASE)
        await queue.release(engine, job)
        return (await queue.claim(engine, LEASE)).attempts

    assert run(_with_engine(db, scenario)) == 1


def _outbox_row(db, event_type="test.event"):
    envelope = {"event_id": "e1", "event_type": event_type, "aggregate": {"id": "a1"}}
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO outbox (id, event_type, schema_version, envelope_json, created_at)"
            " VALUES ('o1', ?, 1, ?, '2026-01-01T00:00:00+00:00')",
            (event_type, json.dumps(envelope)),
        )


def test_worker_fans_outbox_into_jobs_and_runs_them(db):
    _outbox_row(db)
    seen = []

    async def handler(payload):
        seen.append(payload["event"]["aggregate"]["id"])

    async def scenario(engine):
        worker = Worker(engine, {"test.kind": handler}, {"test.event": ["test.kind"]})
        assert await worker.drain_outbox() == 1
        assert await worker.drain_outbox() == 0  # processed rows are not delivered again
        assert await worker.run_one()
        assert not await worker.run_one()

    run(_with_engine(db, scenario))
    assert seen == ["a1"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT processed_at IS NOT NULL FROM outbox").fetchone() == (1,)
        assert conn.execute("SELECT state FROM jobs").fetchall() == [("succeeded",)]


def test_redelivered_outbox_row_does_not_duplicate_jobs(db):
    """If the process dies after enqueueing but before marking processed, redelivery is a no-op."""
    _outbox_row(db)

    async def scenario(engine):
        worker = Worker(engine, {}, {"test.event": ["test.kind"]})
        await worker.drain_outbox()
        async with engine.begin() as conn:
            await conn.exec_driver_sql("UPDATE outbox SET processed_at = NULL")
        await worker.drain_outbox()

    run(_with_engine(db, scenario))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone() == (1,)


def test_handler_crash_is_retried_and_unknown_kind_dead_letters(db):
    async def crashing(_payload):
        raise RuntimeError("bug")

    async def scenario(engine):
        crash_id = await _enqueue(engine, kind="crashes")
        unknown_id = await _enqueue(engine, kind="nobody.handles.this")
        worker = Worker(engine, {"crashes": crashing}, {})
        await worker.run_one()
        await worker.run_one()
        return crash_id, unknown_id

    crash_id, unknown_id = run(_with_engine(db, scenario))
    assert state_of(db, crash_id) == ("queued", 1, "internal")
    assert state_of(db, unknown_id) == ("dead_letter", 1, "unknown_kind")


def test_worker_loop_starts_and_stops(db):
    _outbox_row(db)

    async def scenario(engine):
        done = asyncio.Event()

        async def handler(_payload):
            done.set()

        worker = Worker(engine, {"test.kind": handler}, {"test.event": ["test.kind"]}, poll_interval=0.05)
        worker.start()
        await asyncio.wait_for(done.wait(), 5)
        await worker.stop()

    run(_with_engine(db, scenario))


def test_job_status_endpoint(client, spaces, settings):
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, schema_version, state, max_attempts, available_at,"
            " created_at, updated_at) VALUES ('00000000-0000-0000-0000-0000000000aa', 'x', '{\"secret\": 1}',"
            " 1, 'dead_letter', 5, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
    res = client.get("/api/v1/jobs/00000000-0000-0000-0000-0000000000aa", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["data"]["state"] == "dead_letter"
    assert "payload" not in res.json()["data"]
    assert client.get("/api/v1/jobs/00000000-0000-0000-0000-0000000000bb", headers=AUTH).status_code == 404
