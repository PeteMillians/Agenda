"""Provider accounts and calendar sources (spec §07, §10). Google only in Phase 3."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.application.context import Ctx, audit, require_space, update_revisioned
from app.db.tables import app_settings, calendar_sources, event_mappings, events, external_accounts, users
from app.domain.clock import new_id, now_iso
from app.domain.errors import Conflict, Invalid, NotFound, Unavailable
from app.domain.policy import Action
from app.integrations.google.client import GoogleClient, Grant, ProviderError
from app.integrations.runtime import GOOGLE_CLIENT_SETTING, Integrations, MissingClient
from app.schemas.integrations import CalendarSourcePatch, OAuthClientIn

GOOGLE_CALLBACK_PATH = "/api/v1/integrations/google/callback"


# --------------------------------------------------------------------------- OAuth client config


async def client_status(ctx: Ctx) -> dict[str, Any]:
    async with ctx.read() as conn:
        raw = await conn.scalar(
            select(app_settings.c.value).where(app_settings.c.key == GOOGLE_CLIENT_SETTING)
        )
    return {"configured": raw is not None, "client_id": json.loads(raw)["client_id"] if raw else None}


async def set_client(ctx: Ctx, data: OAuthClientIn) -> dict[str, Any]:
    value = json.dumps({"client_id": data.client_id, "client_secret": data.client_secret})
    async with ctx.write() as conn:
        await conn.execute(delete(app_settings).where(app_settings.c.key == GOOGLE_CLIENT_SETTING))
        await conn.execute(
            insert(app_settings).values(key=GOOGLE_CLIENT_SETTING, value=value, updated_at=now_iso())
        )
    return {"configured": True, "client_id": data.client_id}


# --------------------------------------------------------------------------- sign-in


async def _client(integrations: Integrations) -> GoogleClient:
    try:
        return await integrations.google_client()
    except MissingClient:
        raise Conflict("Add your Google OAuth client first.", code="OAUTH_CLIENT_MISSING") from None


async def start_google(ctx: Ctx, integrations: Integrations, port: int) -> str:
    if integrations.vault is None:
        raise Unavailable(
            "No credential vault is available, so accounts cannot be connected.", code="VAULT_UNAVAILABLE"
        )
    client = await _client(integrations)
    redirect_uri = f"http://127.0.0.1:{port}{GOOGLE_CALLBACK_PATH}"
    state, challenge = integrations.pending.start("google", redirect_uri)
    return client.authorization_url(redirect_uri, state, challenge)


async def complete_google(integrations: Integrations, state: str, code: str) -> str:
    """The browser's loopback redirect. Exempt from the bearer secret, so it is accepted only with a
    one-time state this process issued. Returns the connected email."""
    pending = integrations.pending.take(state)
    if pending is None or pending.provider != "google":
        raise Invalid("This sign-in link expired or was already used. Start again from Agenda.")
    client = await _client(integrations)
    grant = await client.exchange_code(code, pending.verifier, pending.redirect_uri)
    if not grant.refresh_token or not grant.subject:
        raise Invalid(
            "Google did not grant offline access. Remove Agenda's access in your Google account,"
            " then try again."
        )
    assert integrations.vault is not None
    async with integrations.engine.begin() as conn:
        owner = await conn.scalar(select(users.c.id).limit(1))
        existing = (
            (
                await conn.execute(
                    select(external_accounts).where(
                        external_accounts.c.provider == "google",
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
                    provider="google",
                    provider_subject=grant.subject,
                    email=grant.email or "",
                    granted_scopes=" ".join(grant.scopes),
                    credential_ref=f"google:{account_id}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        integrations.vault.set(f"google:{account_id}", grant.refresh_token)
        integrations.remember(account_id, grant)
    await refresh_calendars_for(integrations, account_id, grant)
    await integrations.enqueue_due(force=True)  # reconnecting resumes enabled calendars right away
    return grant.email or "your Google account"


async def refresh_calendars_for(
    integrations: Integrations, account_id: str, grant: Grant | None = None
) -> None:
    """Mirror the account's calendar list: add new calendars (off until mapped), update names and
    colours, and drop calendars removed at Google together with their imported events."""
    async with integrations.engine.connect() as conn:
        account = (
            (await conn.execute(select(external_accounts).where(external_accounts.c.id == account_id)))
            .mappings()
            .one()
        )
    client = await integrations.google_client()
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
                "name": cal.get("summaryOverride") or cal.get("summary") or cal["id"],
                "color": cal.get("backgroundColor", "#64748b"),
                "timezone": cal.get("timeZone", "UTC"),
                "access_role": cal.get("accessRole", "reader"),
                "is_primary": bool(cal.get("primary")),
            }
            if cal["id"] in known:
                await conn.execute(
                    update(calendar_sources)
                    .where(calendar_sources.c.id == known.pop(cal["id"]))
                    .values(**values)
                )
            else:
                await conn.execute(
                    insert(calendar_sources).values(
                        id=new_id(),
                        external_account_id=account_id,
                        provider_calendar_id=cal["id"],
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
        await _own_account(conn, ctx, account_id)
    try:
        await refresh_calendars_for(integrations, account_id)
    except ProviderError as exc:
        raise Unavailable(
            f"Google could not be reached ({exc.category}).", code="PROVIDER_UNAVAILABLE"
        ) from exc


async def disconnect(ctx: Ctx, integrations: Integrations, account_id: str, *, keep_events: bool) -> None:
    """Revoke at Google, remove the vault entry, and keep the imported events as Agenda's own
    (editable) events or delete them."""
    async with ctx.read() as conn:
        account = await _own_account(conn, ctx, account_id)
    refresh = integrations.vault.get(account["credential_ref"]) if integrations.vault else None
    if refresh:
        with contextlib.suppress(MissingClient):
            await (await integrations.google_client()).revoke(refresh)
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


_SOURCE_QUERY = select(calendar_sources, external_accounts.c.email.label("account_email")).join(
    external_accounts, external_accounts.c.id == calendar_sources.c.external_account_id
)


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
    return await integrations.enqueue_sync(source_id, dedupe=f"manual:{now_iso()}")
