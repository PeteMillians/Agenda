"""Provider accounts and calendar sources (spec §07, §10): Google and Microsoft."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from sqlalchemy import delete, insert, select, update
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
from app.integrations.errors import ProviderError
from app.integrations.model import CalendarProvider, Grant
from app.integrations.runtime import Integrations, MissingClient, can_write, client_setting
from app.integrations.state import load_state
from app.integrations.sync import baseline
from app.integrations.vault import VaultUnavailable
from app.schemas.integrations import CalendarSourcePatch, OAuthClientIn, Provider

NAMES = {"google": "Google", "microsoft": "Microsoft"}
CLIENT_ID = {
    "google": re.compile(r"^[\w.-]+\.apps\.googleusercontent\.com$"),
    "microsoft": re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"),
}
WRITABLE_ROLES = ("owner", "writer")


CALLBACK_PATH = "/api/v1/integrations/{provider}/callback"


def callback_url(provider: Provider, port: int) -> str:
    """Google accepts any loopback address for a desktop client; Microsoft matches the registered
    redirect URI, which for loopback must be `http://localhost` (the port is ignored)."""
    host = "localhost" if provider == "microsoft" else "127.0.0.1"
    return f"http://{host}:{port}{CALLBACK_PATH.format(provider=provider)}"


# --------------------------------------------------------------------------- OAuth client config


async def client_status(ctx: Ctx, integrations: Integrations, provider: Provider) -> dict[str, Any]:
    async with ctx.read() as conn:
        raw = await conn.scalar(
            select(app_settings.c.value).where(app_settings.c.key == client_setting(provider))
        )
    return {
        "configured": raw is not None,
        "client_id": json.loads(raw)["client_id"] if raw else None,
        "vault": integrations.vault.description if integrations.vault else None,
        # what to register in Azure: Microsoft ignores the port of a localhost redirect
        "redirect_uri": f"http://localhost{CALLBACK_PATH.format(provider=provider)}"
        if provider == "microsoft"
        else None,
    }


async def set_client(
    ctx: Ctx, integrations: Integrations, provider: Provider, data: OAuthClientIn
) -> dict[str, Any]:
    if not CLIENT_ID[provider].match(data.client_id):
        hint = "…apps.googleusercontent.com" if provider == "google" else "a GUID like 00000000-0000-…"
        raise Invalid(
            f"That doesn't look like a {NAMES[provider]} client ID ({hint}).", code="INVALID_CLIENT_ID"
        )
    config: dict[str, str] = {"client_id": data.client_id}
    if provider == "google":
        if not data.client_secret:
            raise Invalid("Google desktop clients also need their client secret.", code="INVALID_CLIENT_ID")
        config["client_secret"] = data.client_secret
    async with ctx.write() as conn:
        await conn.execute(delete(app_settings).where(app_settings.c.key == client_setting(provider)))
        await conn.execute(
            insert(app_settings).values(
                key=client_setting(provider), value=json.dumps(config), updated_at=now_iso()
            )
        )
    return await client_status(ctx, integrations, provider)


# --------------------------------------------------------------------------- sign-in


async def _provider(integrations: Integrations, provider: str) -> CalendarProvider:
    try:
        return await integrations.provider(provider)
    except MissingClient:
        raise Conflict(
            f"Add your {NAMES[provider]} OAuth client first.", code="OAUTH_CLIENT_MISSING"
        ) from None


async def start_connect(
    ctx: Ctx, integrations: Integrations, provider: Provider, port: int, *, write: bool
) -> str:
    """`write` asks for event write access too, which two-way sync needs (spec §10)."""
    if integrations.vault is None:
        raise Unavailable(
            "No credential vault is available, so accounts cannot be connected.", code="VAULT_UNAVAILABLE"
        )
    client = await _provider(integrations, provider)
    redirect_uri = callback_url(provider, port)
    state, challenge = integrations.pending.start(provider, redirect_uri)
    return client.authorization_url(redirect_uri, state, challenge, write=write)


async def complete_connect(integrations: Integrations, provider: Provider, state: str, code: str) -> str:
    """The browser's loopback redirect. Exempt from the bearer secret, so it is accepted only with a
    one-time state this process issued. Returns the connected email."""
    pending = integrations.pending.take(state)
    if pending is None or pending.provider != provider:
        raise Invalid("This sign-in link expired or was already used. Start again from Agenda.")
    client = await _provider(integrations, provider)
    grant = await client.exchange_code(code, pending.verifier, pending.redirect_uri)
    if not grant.refresh_token or not grant.subject:
        raise Invalid(
            f"{NAMES[provider]} did not grant offline access. Remove Agenda's access in your account"
            " settings, then try again."
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
    return grant.email or f"your {NAMES[provider]} account"


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
    client = await integrations.provider(account["provider"])
    token = grant.access_token if grant else await integrations.access_token(account)
    calendars = await client.list_calendars(token)
    stamp = now_iso()
    async with integrations.engine.begin() as conn:
        known = {
            r.provider_calendar_id: r.id
            for r in await conn.execute(
                select(calendar_sources.c.id, calendar_sources.c.provider_calendar_id).where(
                    calendar_sources.c.external_account_id == account_id
                )
            )
        }
        for cal in calendars:
            values = {
                "name": cal.name,
                "color": cal.color,
                "timezone": cal.timezone,
                "access_role": cal.access_role,
                "is_primary": cal.is_primary,
            }
            if cal.provider_id in known:
                await conn.execute(
                    update(calendar_sources)
                    .where(calendar_sources.c.id == known.pop(cal.provider_id))
                    .values(**values)
                )
            else:
                await conn.execute(
                    insert(calendar_sources).values(
                        id=new_id(),
                        external_account_id=account_id,
                        provider_calendar_id=cal.provider_id,
                        created_at=stamp,
                        updated_at=stamp,
                        **values,
                    )
                )
        if known:
            await conn.execute(delete(calendar_sources).where(calendar_sources.c.id.in_(known.values())))


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


def _account_out(row: RowMapping) -> dict[str, Any]:
    return {**row, "granted_scopes": row["granted_scopes"].split()}


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
            f"{NAMES[account['provider']]} could not be reached ({exc.category}).",
            code="PROVIDER_UNAVAILABLE",
        ) from exc


async def disconnect(ctx: Ctx, integrations: Integrations, account_id: str, *, keep_events: bool) -> None:
    """Revoke at the provider, remove the vault entry, and keep the imported events as Agenda's own
    (editable) events or delete them. Changes not pushed yet stay only in Agenda."""
    async with ctx.read() as conn:
        account = await _own_account(conn, ctx, account_id)
    refresh = integrations.vault.get(account["credential_ref"]) if integrations.vault else None
    if refresh:
        with contextlib.suppress(MissingClient):
            await (await integrations.provider(account["provider"])).revoke(refresh)
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
                .values(calendar_source_id=None)
            )
        await conn.execute(delete(external_accounts).where(external_accounts.c.id == account_id))
        await audit(conn, ctx, "account.disconnected", "external_account", account_id, None)


# --------------------------------------------------------------------------- calendar sources


_SOURCE_QUERY = select(
    calendar_sources,
    external_accounts.c.email.label("account_email"),
    external_accounts.c.provider,
    external_accounts.c.granted_scopes,
).join(external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id)


async def _source_out(conn: AsyncConnection, row: RowMapping) -> dict[str, Any]:
    out = dict(row)
    out["can_write"] = can_write(row["provider"], out.pop("granted_scopes").split())
    state = await load_state(conn, row["id"])
    out["pending_changes"] = state.pending() if row["sync_mode"] == "two_way" else 0
    out["open_conflicts"] = len(state.conflicts)
    return out


async def list_sources(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.read() as conn:
        rows = await conn.execute(
            _SOURCE_QUERY.where(external_accounts.c.user_id == ctx.user_id).order_by(
                external_accounts.c.email, calendar_sources.c.is_primary.desc(), calendar_sources.c.name
            )
        )
        return [await _source_out(conn, r) for r in rows.mappings()]


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


async def _discard_local_changes(conn: AsyncConnection, source_id: str) -> None:
    """Leaving two-way sync: whatever was not pushed is dropped, and a full sync restores the
    provider's version of everything (the UI warns first)."""
    state = await load_state(conn, source_id)
    unpushed = [
        event_id
        for event_id, row in state.rows.items()
        if event_id not in state.mappings and not (row["series_id"] and row["status"] == "cancelled")
    ]
    if unpushed:
        await conn.execute(delete(events).where(events.c.id.in_(unpushed)))
    await conn.execute(
        update(events)
        .where(events.c.calendar_source_id == source_id, events.c.deleted_at.is_not(None))
        .values(deleted_at=None)
    )
    await conn.execute(
        update(event_mappings)
        .where(event_mappings.c.calendar_source_id == source_id)
        .values(etag=None, synced_snapshot=None)
    )
    await conn.execute(
        update(sync_conflicts)
        .where(sync_conflicts.c.calendar_source_id == source_id, sync_conflicts.c.status == "open")
        .values(status="superseded")
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
        if mode == "two_way" and source["sync_mode"] != "two_way":
            if source["access_role"] not in WRITABLE_ROLES:
                raise Conflict(
                    "You can only view this calendar, so it can't be edited here.", code="CALENDAR_READ_ONLY"
                )
            if not can_write(source["provider"], source["granted_scopes"].split()):
                raise Conflict(
                    f"Allow Agenda to edit your {NAMES[source['provider']]} events first.",
                    code="WRITE_ACCESS_REQUIRED",
                )
            await baseline(conn, source_id)  # everything imported so far is in step
        elif mode == "read_only" and source["sync_mode"] == "two_way":
            await _discard_local_changes(conn, source_id)
            values["sync_cursor"] = None
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
        out = await _source_out(conn, row)
    if row["enabled"] and (
        not source["enabled"] or row["last_synced_at"] is None or row["sync_mode"] != source["sync_mode"]
    ):
        await integrations.enqueue_sync(source_id, dedupe=f"change:{row['revision']}")
    return out


async def sync_now(ctx: Ctx, integrations: Integrations, source_id: str) -> str:
    async with ctx.read() as conn:
        source = await _own_source(conn, ctx, source_id)
    if not source["enabled"]:
        raise Conflict("Turn this calendar on before syncing it.", code="CALENDAR_DISABLED")
    return await integrations.enqueue_sync(source_id, dedupe=f"manual:{now_iso()}")
