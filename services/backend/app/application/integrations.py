"""Provider accounts, calendar sources, and sync conflicts (spec §07, §10). Google and Microsoft."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import Ctx, audit, require_space, update_revisioned
from app.db.tables import (
    app_settings,
    calendar_sources,
    event_mappings,
    events,
    external_accounts,
    sync_conflicts,
    users,
)
from app.domain.clock import new_id, now_iso
from app.domain.errors import Conflict, Invalid, NotFound, Unavailable
from app.domain.policy import Action
from app.integrations.base import WRITABLE_ROLES, Grant, ProviderClient, ProviderError, Unsupported
from app.integrations.conflicts import local_snapshot
from app.integrations.providers import CODECS, LABELS
from app.integrations.runtime import CLIENT_SETTINGS, Integrations, MissingClient
from app.integrations.sync import apply_changes
from app.integrations.vault import VaultUnavailable
from app.schemas.integrations import CalendarSourcePatch, MicrosoftClientIn, OAuthClientIn

CALLBACK_PATHS = {
    "google": "/api/v1/integrations/google/callback",
    "microsoft": "/api/v1/integrations/microsoft/callback",
}
GOOGLE_CALLBACK_PATH = CALLBACK_PATHS["google"]
# Entra ignores the port only for `localhost` redirect URIs, so Microsoft gets that host (ADR 0005)
CALLBACK_HOSTS = {"google": "127.0.0.1", "microsoft": "localhost"}


def redirect_uri(provider: str, port: int | str | None) -> str:
    host = CALLBACK_HOSTS[provider] + (f":{port}" if port is not None else "")
    return f"http://{host}{CALLBACK_PATHS[provider]}"


# --------------------------------------------------------------------------- OAuth client config


async def client_status(ctx: Ctx, integrations: Integrations, provider: str = "google") -> dict[str, Any]:
    async with ctx.read() as conn:
        raw = await conn.scalar(
            select(app_settings.c.value).where(app_settings.c.key == CLIENT_SETTINGS[provider])
        )
    config = json.loads(raw) if raw else {}
    return {
        "configured": raw is not None,
        "client_id": config.get("client_id"),
        "tenant": config.get("tenant") if provider == "microsoft" else None,
        "vault": integrations.vault.description if integrations.vault else None,
        # what the owner registers: Microsoft matches localhost redirects on any port
        "redirect_uri": redirect_uri(provider, None if provider == "microsoft" else "<port>"),
    }


async def _save_client(ctx: Ctx, provider: str, value: dict[str, Any]) -> None:
    key = CLIENT_SETTINGS[provider]
    async with ctx.write() as conn:
        await conn.execute(delete(app_settings).where(app_settings.c.key == key))
        await conn.execute(
            insert(app_settings).values(key=key, value=json.dumps(value), updated_at=now_iso())
        )


async def set_client(ctx: Ctx, integrations: Integrations, data: OAuthClientIn) -> dict[str, Any]:
    await _save_client(ctx, "google", {"client_id": data.client_id, "client_secret": data.client_secret})
    return await client_status(ctx, integrations, "google")


async def set_microsoft_client(
    ctx: Ctx, integrations: Integrations, data: MicrosoftClientIn
) -> dict[str, Any]:
    await _save_client(ctx, "microsoft", {"client_id": data.client_id, "tenant": data.tenant})
    return await client_status(ctx, integrations, "microsoft")


# --------------------------------------------------------------------------- sign-in


async def _client(integrations: Integrations, provider: str) -> ProviderClient:
    try:
        return await integrations.client(provider)
    except MissingClient:
        raise Conflict(
            f"Add your {LABELS[provider]} app registration first.", code="OAUTH_CLIENT_MISSING"
        ) from None


async def start(ctx: Ctx, integrations: Integrations, provider: str, port: int, *, write: bool) -> str:
    if integrations.vault is None:
        raise Unavailable(
            "No credential vault is available, so accounts cannot be connected.", code="VAULT_UNAVAILABLE"
        )
    client = await _client(integrations, provider)
    uri = redirect_uri(provider, port)
    state, challenge = integrations.pending.start(provider, uri)
    return client.authorization_url(uri, state, challenge, write=write)


async def start_google(ctx: Ctx, integrations: Integrations, port: int) -> str:
    return await start(ctx, integrations, "google", port, write=False)


async def complete(integrations: Integrations, provider: str, state: str, code: str) -> str:
    """The browser's loopback redirect. Exempt from the bearer secret, so it is accepted only with a
    one-time state this process issued. Returns the connected email."""
    pending = integrations.pending.take(state)
    if pending is None or pending.provider != provider:
        raise Invalid("This sign-in link expired or was already used. Start again from Agenda.")
    client = await _client(integrations, provider)
    grant = await client.exchange_code(code, pending.verifier, pending.redirect_uri)
    if not grant.refresh_token or not grant.subject:
        raise Invalid(
            f"{LABELS[provider]} did not grant offline access. Remove Agenda's access in your account,"
            " then try again."
        )
    assert integrations.vault is not None
    async with integrations.engine.begin() as conn:
        owner = await conn.scalar(select(users.c.id).limit(1))
        existing = (
            (
                await conn.execute(
                    select(external_accounts).where(
                        external_accounts.c.provider == provider,
                        external_accounts.c.provider_subject == grant.subject,
                    )
                )
            )
            .mappings()
            .first()
        )
        stamp = now_iso()
        if existing:  # reconnect: fresh token, back to active
            account_id = existing["id"]
            await conn.execute(
                update(external_accounts)
                .where(external_accounts.c.id == account_id)
                .values(
                    status="active",
                    email=grant.email or existing["email"],
                    granted_scopes=" ".join(grant.scopes),
                    revision=external_accounts.c.revision + 1,
                    updated_at=stamp,
                )
            )
            await conn.execute(
                update(calendar_sources)
                .where(
                    calendar_sources.c.external_account_id == account_id,
                    calendar_sources.c.sync_state == "needs_reauth",
                )
                .values(sync_state="idle", last_error=None)
            )
        else:
            account_id = new_id()
            await conn.execute(
                insert(external_accounts).values(
                    id=account_id,
                    user_id=owner,
                    provider=provider,
                    provider_subject=grant.subject,
                    email=grant.email or "",
                    granted_scopes=" ".join(grant.scopes),
                    credential_ref=f"{provider}:{account_id}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        try:
            integrations.vault.set(f"{provider}:{account_id}", grant.refresh_token)
        except VaultUnavailable as exc:  # rolls the account row back with the transaction
            raise Unavailable(
                "The credential vault refused to store the token.", code="VAULT_UNAVAILABLE"
            ) from exc
        integrations.remember(account_id, grant)
    await refresh_calendars_for(integrations, account_id, grant)
    await integrations.enqueue_due(force=True)  # reconnecting resumes enabled calendars right away
    return grant.email or f"your {LABELS[provider]} account"


async def complete_google(integrations: Integrations, state: str, code: str) -> str:
    return await complete(integrations, "google", state, code)


async def refresh_calendars_for(
    integrations: Integrations, account_id: str, grant: Grant | None = None
) -> None:
    """Mirror the account's calendar list: add new calendars (off until mapped), update names and
    colours, and drop calendars removed at the provider together with their imported events."""
    async with integrations.engine.connect() as conn:
        account = (
            (await conn.execute(select(external_accounts).where(external_accounts.c.id == account_id)))
            .mappings()
            .one()
        )
        owner_tz = await conn.scalar(select(users.c.timezone).where(users.c.id == account["user_id"]))
    client = await integrations.client(account["provider"])
    token = grant.access_token if grant else await integrations.access_token(account)
    calendars = await client.list_calendars(token)
    stamp = now_iso()
    async with integrations.engine.begin() as conn:
        known = {
            r.provider_calendar_id: (r.id, r.sync_mode)
            for r in await conn.execute(
                select(
                    calendar_sources.c.id,
                    calendar_sources.c.provider_calendar_id,
                    calendar_sources.c.sync_mode,
                ).where(calendar_sources.c.external_account_id == account_id)
            )
        }
        for cal in calendars:
            values: dict[str, Any] = {
                "name": cal.name,
                "color": cal.color,
                "timezone": cal.timezone or owner_tz or "UTC",
                "access_role": cal.access_role,
                "is_primary": cal.is_primary,
            }
            if cal.provider_calendar_id in known:
                source_id, mode = known.pop(cal.provider_calendar_id)
                if mode == "two_way" and cal.access_role not in WRITABLE_ROLES:
                    values["sync_mode"] = "read_only"  # write access was taken away at the provider
                await conn.execute(
                    update(calendar_sources).where(calendar_sources.c.id == source_id).values(**values)
                )
            else:
                await conn.execute(
                    insert(calendar_sources).values(
                        id=new_id(),
                        external_account_id=account_id,
                        provider_calendar_id=cal.provider_calendar_id,
                        created_at=stamp,
                        updated_at=stamp,
                        **values,
                    )
                )
        if known:
            await conn.execute(
                delete(calendar_sources).where(calendar_sources.c.id.in_([sid for sid, _ in known.values()]))
            )


# --------------------------------------------------------------------------- accounts


async def _own_account(conn: AsyncConnection, ctx: Ctx, account_id: str) -> RowMapping:
    row = (
        (
            await conn.execute(
                select(external_accounts).where(
                    external_accounts.c.id == account_id, external_accounts.c.user_id == ctx.user_id
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Account")
    return row


def _can_write(provider: str, scopes: list[str]) -> bool:
    if provider == "microsoft":
        return any(s.lower().endswith("calendars.readwrite") for s in scopes)
    return any(s.endswith(("/auth/calendar.events", "/auth/calendar")) for s in scopes)


def _account_out(row: RowMapping) -> dict[str, Any]:
    scopes = row["granted_scopes"].split()
    return {**row, "granted_scopes": scopes, "can_write": _can_write(row["provider"], scopes)}


async def list_accounts(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.read() as conn:
        rows = await conn.execute(
            select(external_accounts)
            .where(external_accounts.c.user_id == ctx.user_id)
            .order_by(external_accounts.c.created_at)
        )
        return [_account_out(r) for r in rows.mappings()]


async def refresh_calendars(ctx: Ctx, integrations: Integrations, account_id: str) -> None:
    async with ctx.read() as conn:
        account = await _own_account(conn, ctx, account_id)
    try:
        await refresh_calendars_for(integrations, account_id)
    except ProviderError as exc:
        raise Unavailable(
            f"{LABELS[account['provider']]} could not be reached ({exc.category}).",
            code="PROVIDER_UNAVAILABLE",
        ) from exc


async def disconnect(ctx: Ctx, integrations: Integrations, account_id: str, *, keep_events: bool) -> None:
    """Revoke at the provider (where it can be), remove the vault entry, and keep the imported events
    as Agenda's own (editable) events or delete them."""
    async with ctx.read() as conn:
        account = await _own_account(conn, ctx, account_id)
    refresh = integrations.vault.get(account["credential_ref"]) if integrations.vault else None
    if refresh:
        with contextlib.suppress(MissingClient, ProviderError):
            await (await integrations.client(account["provider"])).revoke(refresh)
    if integrations.vault:
        integrations.vault.delete(account["credential_ref"])
    integrations.forget(account_id)
    async with ctx.write() as conn:
        source_ids = select(calendar_sources.c.id).where(calendar_sources.c.external_account_id == account_id)
        if keep_events:
            await conn.execute(
                delete(event_mappings).where(event_mappings.c.calendar_source_id.in_(source_ids))
            )
            await conn.execute(
                update(events)
                .where(events.c.calendar_source_id.in_(source_ids))
                .values(calendar_source_id=None, needs_push=False)
            )
        await conn.execute(delete(external_accounts).where(external_accounts.c.id == account_id))
        await audit(conn, ctx, "account.disconnected", "external_account", account_id, None)


# --------------------------------------------------------------------------- calendar sources


_PENDING = (
    select(func.count())
    .where(events.c.calendar_source_id == calendar_sources.c.id, events.c.needs_push.is_(True))
    .correlate(calendar_sources)
    .scalar_subquery()
)
_CONFLICTS = (
    select(func.count())
    .where(sync_conflicts.c.calendar_source_id == calendar_sources.c.id, sync_conflicts.c.status == "open")
    .correlate(calendar_sources)
    .scalar_subquery()
)
_SOURCE_QUERY = select(
    calendar_sources,
    external_accounts.c.email.label("account_email"),
    external_accounts.c.provider,
    external_accounts.c.granted_scopes,
    _PENDING.label("pending_changes"),
    _CONFLICTS.label("open_conflicts"),
).join(external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id)


async def list_sources(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.read() as conn:
        rows = await conn.execute(
            _SOURCE_QUERY.where(external_accounts.c.user_id == ctx.user_id).order_by(
                external_accounts.c.email, calendar_sources.c.is_primary.desc(), calendar_sources.c.name
            )
        )
        return [dict(r) for r in rows.mappings()]


async def _own_source(conn: AsyncConnection, ctx: Ctx, source_id: str) -> RowMapping:
    row = (
        (
            await conn.execute(
                _SOURCE_QUERY.where(
                    calendar_sources.c.id == source_id, external_accounts.c.user_id == ctx.user_id
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Calendar")
    return row


def _unsent(source: RowMapping) -> Conflict:
    label = LABELS[source["provider"]]
    return Conflict(
        f"{source['pending_changes']} change(s) haven't reached {label} yet and"
        f" {source['open_conflicts']} conflict(s) are open. Sync and resolve them first.",
        code="PENDING_CHANGES",
    )


async def update_source(
    ctx: Ctx, integrations: Integrations, source_id: str, expected: int, data: CalendarSourcePatch
) -> dict[str, Any]:
    sent = data.model_fields_set
    async with ctx.write() as conn:
        source = await _own_source(conn, ctx, source_id)
        values: dict[str, Any] = {}
        space_id = str(data.space_id) if data.space_id else None
        if "space_id" in sent:
            if space_id:
                await require_space(conn, ctx, space_id, Action.WRITE)
            values["space_id"] = space_id
        enabled = data.enabled if "enabled" in sent and data.enabled is not None else source["enabled"]
        target_space = values.get("space_id", source["space_id"])
        if enabled and not target_space:
            raise Invalid("Choose a space for this calendar first.", code="SPACE_REQUIRED")
        values["enabled"] = enabled
        mode = data.sync_mode if "sync_mode" in sent and data.sync_mode else source["sync_mode"]
        if not enabled:
            mode = "read_only"
        unsent = source["pending_changes"] or source["open_conflicts"]
        if source["sync_mode"] == "two_way" and mode == "read_only" and unsent:
            raise _unsent(source)  # read-only would let the next sync overwrite them
        if mode == "two_way" and source["sync_mode"] != "two_way":
            label = LABELS[source["provider"]]
            if source["access_role"] not in WRITABLE_ROLES:
                raise Conflict(f"{label} only lets you read this calendar.", code="READ_ONLY_CALENDAR")
            if not _can_write(source["provider"], source["granted_scopes"].split()):
                raise Conflict(
                    f"Agenda needs permission to change {label} events. Grant write access, then try again.",
                    {"provider": source["provider"], "external_account_id": source["external_account_id"]},
                    code="WRITE_ACCESS_REQUIRED",
                )
        values["sync_mode"] = mode
        if not enabled and source["enabled"]:
            # turning import off drops the copies; the next enable starts with a full sync
            await conn.execute(delete(events).where(events.c.calendar_source_id == source_id))
            values.update(sync_cursor=None, sync_state="idle", last_error=None, last_synced_at=None)
        elif enabled and target_space != source["space_id"] and source["space_id"] is not None:
            # moving a calendar to another space moves its events (spec §10)
            await conn.execute(
                update(events).where(events.c.calendar_source_id == source_id).values(space_id=target_space)
            )
        await update_revisioned(conn, calendar_sources, source_id, expected, values)
        row = await _own_source(conn, ctx, source_id)
    if row["enabled"] and (not source["enabled"] or row["last_synced_at"] is None):
        await integrations.enqueue_sync(source_id, dedupe=f"enable:{row['revision']}")
    return dict(row)


async def sync_now(ctx: Ctx, integrations: Integrations, source_id: str) -> str:
    async with ctx.read() as conn:
        source = await _own_source(conn, ctx, source_id)
    if not source["enabled"]:
        raise Conflict("Turn this calendar on before syncing it.", code="CALENDAR_DISABLED")
    if source["sync_mode"] == "two_way" and source["pending_changes"]:
        await integrations.enqueue_push(source_id, dedupe=f"manual:{now_iso()}")
    return await integrations.enqueue_sync(source_id, dedupe=f"manual:{now_iso()}")


# --------------------------------------------------------------------------- conflicts


def _version(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": snapshot["title"],
        "description": snapshot.get("description", ""),
        "location": snapshot.get("location", ""),
        "all_day": snapshot.get("start_date") is not None,
        "start_at": snapshot.get("start_at"),
        "end_at": snapshot.get("end_at"),
        "start_date": snapshot.get("start_date"),
        "end_date": snapshot.get("end_date"),
        "timezone": snapshot["timezone"],
        "rrule": snapshot.get("rrule"),
        "deleted": snapshot.get("deleted", False),
    }


def _remote_version(provider: str, raw: str | None, timezone: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        incoming = CODECS[provider].parse(json.loads(raw), timezone)
    except (Unsupported, KeyError, ValueError):
        return None
    if incoming.cancelled:
        return None
    return _version({**incoming.content, **(incoming.timing or {}), "rrule": incoming.rrule})


_CONFLICT_QUERY = (
    select(
        sync_conflicts,
        calendar_sources.c.name.label("calendar_name"),
        calendar_sources.c.timezone.label("calendar_timezone"),
        external_accounts.c.provider,
    )
    .join(calendar_sources, calendar_sources.c.id == sync_conflicts.c.calendar_source_id)
    .join(external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id)
)


async def list_conflicts(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.read() as conn:
        rows = list(
            (
                await conn.execute(
                    _CONFLICT_QUERY.where(
                        external_accounts.c.user_id == ctx.user_id, sync_conflicts.c.status == "open"
                    ).order_by(sync_conflicts.c.detected_at)
                )
            ).mappings()
        )
        found = []
        for row in rows:
            event = (
                (await conn.execute(select(events).where(events.c.id == row["event_id"]))).mappings().one()
            )
            remote = _remote_version(row["provider"], row["remote_snapshot"], row["calendar_timezone"])
            local = local_snapshot(event)  # as it is now, not as it was when detected
            found.append(
                {
                    **row,
                    "local": _version(local),
                    "remote": remote,
                    "can_keep_both": _can_keep_both(event, remote),
                }
            )
        return found


def _can_keep_both(event: RowMapping, remote: dict[str, Any] | None) -> bool:
    """Two independent single events; a series or a deletion has no meaningful second copy."""
    return (
        remote is not None
        and event["deleted_at"] is None
        and event["rrule"] is None
        and event["series_id"] is None
    )


async def resolve_conflict(ctx: Ctx, integrations: Integrations, conflict_id: str, resolution: str) -> None:
    """keep_local re-sends Agenda's version over the provider's current one; keep_remote applies the
    provider's version; keep_both keeps Agenda's version as a new, separate event and applies the
    provider's to the original. Then the push job reconciles the provider (spec §10)."""
    async with ctx.write() as conn:
        conflict = (
            (
                await conn.execute(
                    _CONFLICT_QUERY.where(
                        sync_conflicts.c.id == conflict_id, external_accounts.c.user_id == ctx.user_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if conflict is None:
            raise NotFound("Conflict")
        if conflict["status"] != "open":
            raise Conflict("This conflict was already resolved.", code="CONFLICT_RESOLVED")
        event = (
            (await conn.execute(select(events).where(events.c.id == conflict["event_id"]))).mappings().one()
        )
        source = (
            (
                await conn.execute(
                    select(calendar_sources).where(calendar_sources.c.id == conflict["calendar_source_id"])
                )
            )
            .mappings()
            .one()
        )
        remote = json.loads(conflict["remote_snapshot"]) if conflict["remote_snapshot"] else None
        if resolution == "keep_both" and not _can_keep_both(
            event, _remote_version(conflict["provider"], conflict["remote_snapshot"], source["timezone"])
        ):
            raise Invalid(
                "Keep both works for single events that still exist on both sides.", code="CANNOT_KEEP_BOTH"
            )
        await conn.execute(
            update(sync_conflicts)
            .where(sync_conflicts.c.id == conflict_id)
            .values(status="resolved", resolution=resolution, resolved_at=now_iso())
        )
        if resolution == "keep_local":
            if remote is None:  # deleted there: send ours as a new provider event
                await conn.execute(delete(event_mappings).where(event_mappings.c.event_id == event["id"]))
            else:  # overwrite their current version, which we now know
                await conn.execute(
                    update(event_mappings)
                    .where(event_mappings.c.event_id == event["id"])
                    .values(etag=conflict["remote_version"], last_synced_revision=0)
                )
            await conn.execute(update(events).where(events.c.id == event["id"]).values(needs_push=True))
        else:
            if resolution == "keep_both":
                await _copy_as_new(conn, event)
            await _take_remote(conn, source, event, remote, conflict["provider"])
        await audit(conn, ctx, f"conflict.{resolution}", "event", event["id"], event["space_id"])
    await integrations.enqueue_push(source["id"], dedupe=f"resolve:{conflict_id}")


async def _copy_as_new(conn: AsyncConnection, event: RowMapping) -> None:
    """Agenda's version becomes its own event; the push job creates it at the provider."""
    stamp = now_iso()
    values = {
        k: v
        for k, v in event.items()
        if k not in ("id", "revision", "created_at", "updated_at", "needs_push")
    }
    values["title"] = f"{event['title']} (Agenda copy)"[:200]
    await conn.execute(
        insert(events).values(id=new_id(), **values, needs_push=True, created_at=stamp, updated_at=stamp)
    )


async def _take_remote(
    conn: AsyncConnection, source: RowMapping, event: RowMapping, remote: dict[str, Any] | None, provider: str
) -> None:
    """Discard Agenda's unsent edits to `event` and apply the provider's version."""
    series = (events.c.id == event["id"]) | (events.c.series_id == event["id"])
    await conn.execute(update(events).where(series).values(needs_push=False))
    if remote is None:  # deleted there: delete here too
        await conn.execute(delete(events).where(events.c.id == event["id"]))
        return
    if event["deleted_at"] is not None:  # deleted here, changed there: bring it back
        await conn.execute(update(events).where(series).values(deleted_at=None))
    # forget the stored ETag so the provider's copy is applied, not skipped as unchanged
    await conn.execute(
        update(event_mappings).where(event_mappings.c.event_id == event["id"]).values(etag=None)
    )
    await apply_changes(conn, source, [remote], CODECS[provider], full=False)
