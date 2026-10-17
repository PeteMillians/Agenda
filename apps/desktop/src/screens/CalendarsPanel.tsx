import type { CalendarSource, ExternalAccount } from "@agenda/contracts";
import { useEffect, useRef, useState } from "react";
import { api, openExternal } from "../api/backend";
import { call, describe, ifMatch, notifyChanged, useAction, useLoad } from "../api/call";
import { useSpaces } from "../state/spaces";

const SIGN_IN_TIMEOUT_MS = 5 * 60 * 1000;

/** Provider accounts and which space each calendar feeds (spec §07, §10). */
export function CalendarsPanel() {
  const client = useLoad(async () => (await call((await api()).GET("/api/v1/integrations/google/client"))).data, []);
  const accounts = useLoad(async () => (await call((await api()).GET("/api/v1/external-accounts"))).data, []);
  const sources = useLoad(async () => (await call((await api()).GET("/api/v1/calendar-sources"))).data, []);
  const [editingClient, setEditingClient] = useState(false);

  if (!client.data) return null;
  const showClientForm = !client.data.configured || editingClient;

  return (
    <div className="page-wide">
      <h2>Calendars</h2>
      <p className="muted">
        Agenda reads your Google calendars and shows them in the space you choose. It checks for changes every few
        minutes while it's running. Changes made in Google show up here; editing them from Agenda comes later.
      </p>
      {!client.data.vault && (
        <p className="problem">
          This computer has no credential vault Agenda can use, so Google accounts can't be connected. Agenda never
          stores sign-in tokens unprotected.
        </p>
      )}
      {client.data.vault?.includes("NOT a secure") && (
        <p className="muted">Development build: tokens are kept in a plain file in the dev data folder.</p>
      )}
      {showClientForm ? (
        <ClientSetup
          clientId={client.data.client_id}
          onSaved={async () => {
            setEditingClient(false);
            await client.reload();
          }}
          onCancel={client.data.configured ? () => setEditingClient(false) : undefined}
        />
      ) : (
        <>
          <ConnectGoogle accounts={accounts.data ?? []} onConnected={async () => (await accounts.reload(), await sources.reload())} />
          {(accounts.data ?? []).map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              sources={(sources.data ?? []).filter((s) => s.external_account_id === account.id)}
              onChanged={async () => {
                await accounts.reload();
                await sources.reload();
              }}
            />
          ))}
          <p className="muted">
            Google OAuth client: <code>{client.data.client_id}</code>{" "}
            <button className="quiet" onClick={() => setEditingClient(true)}>
              Change
            </button>
          </p>
        </>
      )}
      {(accounts.error || sources.error || client.error) && (
        <p className="problem">{accounts.error ?? sources.error ?? client.error}</p>
      )}
    </div>
  );
}

function ClientSetup(props: { clientId: string | null; onSaved: () => void; onCancel?: () => void }) {
  const [clientId, setClientId] = useState(props.clientId ?? "");
  const [secret, setSecret] = useState("");
  const [problem, setProblem] = useState<string>();
  return (
    <form
      className="card stack"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await call(
            (await api()).PUT("/api/v1/integrations/google/client", { body: { client_id: clientId, client_secret: secret } }),
          );
          props.onSaved();
        } catch (err) {
          setProblem(describe(err));
        }
      }}
    >
      <h3>One-time setup: your Google OAuth client</h3>
      <ol className="muted steps">
        <li>
          In Google Cloud Console, create a project and enable the <strong>Google Calendar API</strong>.
        </li>
        <li>
          Under <strong>OAuth consent screen</strong>, choose External, leave it in <strong>Testing</strong>, and add
          your Google address as a test user.
        </li>
        <li>
          Under <strong>Credentials</strong>, create an OAuth client ID of type <strong>Desktop app</strong>, then paste
          its ID and secret here. Google treats a desktop app's secret as non-confidential.
        </li>
      </ol>
      <p className="muted">
        In Testing mode Google asks you to sign in again every 7 days. Agenda will tell you when that's needed.
      </p>
      <label>
        Client ID
        <input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="…apps.googleusercontent.com" />
      </label>
      <label>
        Client secret
        <input value={secret} onChange={(e) => setSecret(e.target.value)} type="password" autoComplete="off" />
      </label>
      <div className="form-grid">
        <button type="submit" disabled={!clientId.trim() || !secret.trim()}>
          Save
        </button>
        {props.onCancel && (
          <button type="button" className="quiet" onClick={props.onCancel}>
            Cancel
          </button>
        )}
      </div>
      {problem && <p className="problem">{problem}</p>}
    </form>
  );
}

/** Opens Google's sign-in in the system browser, then watches for the account to appear. */
function ConnectGoogle(props: { accounts: ExternalAccount[]; onConnected: () => Promise<void> }) {
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<string>();
  const before = useRef("");
  const signature = (list: ExternalAccount[]) => list.map((a) => `${a.id}:${a.revision}`).join();

  useEffect(() => {
    if (!waiting) return;
    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const now = (await call((await api()).GET("/api/v1/external-accounts"))).data;
        if (signature(now) !== before.current) {
          setWaiting(false);
          await props.onConnected();
        } else if (Date.now() - started > SIGN_IN_TIMEOUT_MS) {
          setWaiting(false);
          setProblem("Sign-in timed out. Try again.");
        }
      } catch {
        // keep waiting; the backend may be restarting
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [waiting, props]);

  return (
    <div className="card">
      {waiting ? (
        <p>
          Finish signing in to Google in your browser…{" "}
          <button className="quiet" onClick={() => setWaiting(false)}>
            Cancel
          </button>
        </p>
      ) : (
        <button
          onClick={async () => {
            setProblem(undefined);
            try {
              const started = await call((await api()).POST("/api/v1/integrations/google/connect/start"));
              before.current = signature(props.accounts);
              await openExternal(started.data.authorization_url);
              setWaiting(true);
            } catch (err) {
              setProblem(describe(err));
            }
          }}
        >
          {props.accounts.length ? "Connect another Google account" : "Connect Google Calendar"}
        </button>
      )}
      {problem && <p className="problem">{problem}</p>}
    </div>
  );
}

function AccountCard(props: { account: ExternalAccount; sources: CalendarSource[]; onChanged: () => Promise<void> }) {
  const { account } = props;
  const { run, error } = useAction(props.onChanged);
  const [disconnecting, setDisconnecting] = useState(false);
  return (
    <div className="card">
      <div className="list-head">
        <strong className="grow">{account.email}</strong>
        <button
          className="quiet"
          onClick={() =>
            run(async () =>
              call(
                (await api()).POST("/api/v1/external-accounts/{account_id}/refresh-calendars", {
                  params: { path: { account_id: account.id } },
                }),
              ),
            )
          }
        >
          Refresh calendar list
        </button>
        {!disconnecting && (
          <button className="quiet danger" onClick={() => setDisconnecting(true)}>
            Disconnect…
          </button>
        )}
      </div>
      {account.status === "needs_reauth" && (
        <p className="problem">Google needs you to sign in again. Use "Connect Google Calendar" with this account.</p>
      )}
      {disconnecting && (
        <div className="scope">
          Disconnect {account.email}?
          {(["keep", "remove"] as const).map((choice) => (
            <button
              key={choice}
              onClick={() =>
                run(async () =>
                  call(
                    (await api()).DELETE("/api/v1/external-accounts/{account_id}", {
                      params: { path: { account_id: account.id }, query: { keep_events: choice === "keep" } },
                    }),
                  ),
                )
              }
            >
              {choice === "keep" ? "Keep its events in Agenda" : "Remove its events"}
            </button>
          ))}
          <button className="quiet" onClick={() => setDisconnecting(false)}>
            Cancel
          </button>
        </div>
      )}
      <ul className="rows">
        {props.sources.map((source) => (
          <SourceRow key={source.id} source={source} run={run} />
        ))}
      </ul>
      {error && <p className="problem">{error}</p>}
    </div>
  );
}

function SourceRow({ source, run }: { source: CalendarSource; run: (action: () => Promise<unknown>) => Promise<void> }) {
  const { spaces } = useSpaces();
  const patch = (body: { space_id?: string | null; enabled?: boolean }) =>
    run(async () =>
      call(
        (await api()).PATCH("/api/v1/calendar-sources/{source_id}", {
          params: { path: { source_id: source.id }, header: ifMatch(source.revision) },
          body,
        }),
      ),
    );
  return (
    <li className="row">
      <span className="dot" style={{ background: source.color }} />
      <span className="grow">
        {source.name}
        {source.is_primary && <span className="muted"> · primary</span>}
        <span className="meta">
          <SyncState source={source} />
        </span>
      </span>
      <select
        aria-label={`Space for ${source.name}`}
        value={source.enabled ? (source.space_id ?? "") : ""}
        onChange={(e) => patch(e.target.value ? { space_id: e.target.value, enabled: true } : { enabled: false })}
      >
        <option value="">Don't import</option>
        {spaces.map((space) => (
          <option key={space.id} value={space.id}>
            Into {space.name}
          </option>
        ))}
      </select>
      {source.enabled && (
        <button
          className="quiet"
          onClick={() =>
            run(async () => {
              await call(
                (await api()).POST("/api/v1/calendar-sources/{source_id}/sync", { params: { path: { source_id: source.id } } }),
              );
              notifyChanged();
            })
          }
        >
          Sync now
        </button>
      )}
    </li>
  );
}

const ERRORS: Record<string, string> = {
  needs_reauth: "sign in again",
  rate_limited: "Google asked us to slow down; retrying",
  provider_unavailable: "Google is unavailable; retrying",
  provider_unreachable: "offline; retrying",
  access_denied: "access denied",
  calendar_not_found: "calendar no longer exists",
  oauth_client_missing: "OAuth client missing",
};

function SyncState({ source }: { source: CalendarSource }) {
  if (!source.enabled) return <span>not imported</span>;
  if (source.sync_state === "error" || source.sync_state === "needs_reauth") {
    return <span className="overdue">{ERRORS[source.last_error ?? ""] ?? source.last_error ?? "sync failed"}</span>;
  }
  if (!source.last_synced_at) return <span>waiting for first sync…</span>;
  return <span>synced {new Date(source.last_synced_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>;
}
