import type { CalendarSource, ExternalAccount, OAuthClientStatus, SyncConflict } from "@agenda/contracts";
import { useEffect, useRef, useState } from "react";
import { api, openExternal } from "../api/backend";
import { ApiError, call, describe, ifMatch, notifyChanged, useAction, useLoad } from "../api/call";
import { formatDay, formatTime, localDateOf } from "../dates";
import { useSpaces } from "../state/spaces";

const SIGN_IN_TIMEOUT_MS = 5 * 60 * 1000;

type Provider = "google" | "microsoft";
const LABEL: Record<Provider, string> = { google: "Google", microsoft: "Outlook" };

/** Provider accounts, which space each calendar feeds, two-way sync, and conflicts (spec §07, §10). */
export function CalendarsPanel() {
  const accounts = useLoad(async () => (await call((await api()).GET("/api/v1/external-accounts"))).data, []);
  const sources = useLoad(async () => (await call((await api()).GET("/api/v1/calendar-sources"))).data, []);
  const conflicts = useLoad(async () => (await call((await api()).GET("/api/v1/sync-conflicts"))).data, []);
  const reload = async () => {
    await accounts.reload();
    await sources.reload();
    await conflicts.reload();
  };

  return (
    <div className="page-wide">
      <h2>Calendars</h2>
      <p className="muted">
        Agenda shows your Google and Outlook calendars in the space you choose and checks for changes every few minutes
        while it's running. Turn on two-way sync for a calendar to edit its events here too.
      </p>
      {(conflicts.data ?? []).length > 0 && <Conflicts conflicts={conflicts.data!} onChanged={reload} />}
      {(["google", "microsoft"] as const).map((provider) => (
        <ProviderSection
          key={provider}
          provider={provider}
          accounts={(accounts.data ?? []).filter((a) => a.provider === provider)}
          sources={sources.data ?? []}
          onChanged={reload}
        />
      ))}
      {(accounts.error || sources.error || conflicts.error) && (
        <p className="problem">{accounts.error ?? sources.error ?? conflicts.error}</p>
      )}
    </div>
  );
}

function ProviderSection(props: {
  provider: Provider;
  accounts: ExternalAccount[];
  sources: CalendarSource[];
  onChanged: () => Promise<void>;
}) {
  const { provider } = props;
  const client = useLoad(
    async () =>
      provider === "google"
        ? (await call((await api()).GET("/api/v1/integrations/google/client"))).data
        : (await call((await api()).GET("/api/v1/integrations/microsoft/client"))).data,
    [provider],
  );
  const [editingClient, setEditingClient] = useState(false);
  if (!client.data) return client.error ? <p className="problem">{client.error}</p> : null;
  const showClientForm = !client.data.configured || editingClient;
  const done = async () => {
    setEditingClient(false);
    await client.reload();
  };
  const cancel = client.data.configured ? () => setEditingClient(false) : undefined;

  return (
    <section>
      <h3>{provider === "google" ? "Google Calendar" : "Outlook (Microsoft 365, Outlook.com)"}</h3>
      {!client.data.vault && (
        <p className="problem">
          This computer has no credential vault Agenda can use, so accounts can't be connected. Agenda never stores
          sign-in tokens unprotected.
        </p>
      )}
      {client.data.vault?.includes("NOT a secure") && (
        <p className="muted">Development build: tokens are kept in a plain file in the dev data folder.</p>
      )}
      {showClientForm ? (
        provider === "google" ? (
          <GoogleSetup status={client.data} onSaved={done} onCancel={cancel} />
        ) : (
          <MicrosoftSetup status={client.data} onSaved={done} onCancel={cancel} />
        )
      ) : (
        <>
          <ConnectButton provider={provider} accounts={props.accounts} onConnected={props.onChanged} />
          {props.accounts.map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              sources={props.sources.filter((s) => s.external_account_id === account.id)}
              onChanged={props.onChanged}
            />
          ))}
          <p className="muted">
            {provider === "google" ? "Google OAuth client" : "Microsoft app (client) ID"}: <code>{client.data.client_id}</code>
            {client.data.tenant && client.data.tenant !== "common" && <> · sign-in authority {client.data.tenant}</>}{" "}
            <button className="quiet" onClick={() => setEditingClient(true)}>
              Change
            </button>
          </p>
        </>
      )}
    </section>
  );
}

function GoogleSetup(props: { status: OAuthClientStatus; onSaved: () => void; onCancel?: () => void }) {
  const [clientId, setClientId] = useState(props.status.client_id ?? "");
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
      <FormButtons disabled={!clientId.trim() || !secret.trim()} onCancel={props.onCancel} />
      {problem && <p className="problem">{problem}</p>}
    </form>
  );
}

const AUTHORITIES = [
  { value: "common", label: "Work, school, and personal accounts" },
  { value: "consumers", label: "Personal Microsoft accounts only" },
  { value: "organizations", label: "Work and school accounts only" },
];

function MicrosoftSetup(props: { status: OAuthClientStatus; onSaved: () => void; onCancel?: () => void }) {
  const [clientId, setClientId] = useState(props.status.client_id ?? "");
  const initial = props.status.tenant ?? "common";
  const known = AUTHORITIES.some((a) => a.value === initial);
  const [authority, setAuthority] = useState(known ? initial : "tenant");
  const [tenant, setTenant] = useState(known ? "" : initial);
  const [problem, setProblem] = useState<string>();
  return (
    <form
      className="card stack"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await call(
            (await api()).PUT("/api/v1/integrations/microsoft/client", {
              body: { client_id: clientId, tenant: authority === "tenant" ? tenant : authority },
            }),
          );
          props.onSaved();
        } catch (err) {
          setProblem(describe(err));
        }
      }}
    >
      <h3>One-time setup: register Agenda with Microsoft</h3>
      <ol className="muted steps">
        <li>
          Sign in to the <strong>Microsoft Entra admin center</strong> (entra.microsoft.com) or the Azure portal with
          the Microsoft account you'll connect, and open <strong>App registrations → New registration</strong>.
        </li>
        <li>
          Under <strong>Supported account types</strong>, choose{" "}
          <strong>Accounts in any organizational directory and personal Microsoft accounts</strong> (needed for
          Outlook.com, Hotmail, and Live addresses).
        </li>
        <li>
          Under <strong>Redirect URI</strong>, pick the platform <strong>Public client/native (mobile &amp; desktop)</strong>{" "}
          and enter <code>{props.status.redirect_uri}</code> exactly. Not "Web" or "Single-page application": those
          need a client secret or a browser origin, and sign-in fails.
        </li>
        <li>
          Register, then copy the <strong>Application (client) ID</strong> from the Overview page into the box below.
          Agenda asks for calendar permissions when you sign in; there's no secret to create.
        </li>
      </ol>
      <label>
        Application (client) ID
        <input
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="00000000-0000-0000-0000-000000000000"
        />
      </label>
      <label>
        Accounts the app accepts
        <select value={authority} onChange={(e) => setAuthority(e.target.value)}>
          {AUTHORITIES.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
          <option value="tenant">Only my organization (single tenant)</option>
        </select>
      </label>
      {authority === "tenant" && (
        <label>
          Directory (tenant) ID
          <input value={tenant} onChange={(e) => setTenant(e.target.value)} />
        </label>
      )}
      <p className="muted">Match this to the supported account types you picked when registering the app.</p>
      <FormButtons disabled={!clientId.trim() || (authority === "tenant" && !tenant.trim())} onCancel={props.onCancel} />
      {problem && <p className="problem">{problem}</p>}
    </form>
  );
}

function FormButtons(props: { disabled: boolean; onCancel?: () => void }) {
  return (
    <div className="form-grid">
      <button type="submit" disabled={props.disabled}>
        Save
      </button>
      {props.onCancel && (
        <button type="button" className="quiet" onClick={props.onCancel}>
          Cancel
        </button>
      )}
    </div>
  );
}

/** Starts the provider's sign-in in the system browser, then watches for the account to change. */
function useSignIn(provider: Provider, accounts: ExternalAccount[], onConnected: () => Promise<void>) {
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<string>();
  const before = useRef("");
  const signature = (list: ExternalAccount[]) => list.map((a) => `${a.id}:${a.revision}`).join();

  useEffect(() => {
    if (!waiting) return;
    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const now = (await call((await api()).GET("/api/v1/external-accounts"))).data.filter((a) => a.provider === provider);
        if (signature(now) !== before.current) {
          setWaiting(false);
          await onConnected();
        } else if (Date.now() - started > SIGN_IN_TIMEOUT_MS) {
          setWaiting(false);
          setProblem("Sign-in timed out. If the browser showed an error, check the app registration, then try again.");
        }
      } catch {
        // keep waiting; the backend may be restarting
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [waiting, provider, onConnected]);

  const begin = async (write: boolean) => {
    setProblem(undefined);
    try {
      const started = await call(
        (await api()).POST("/api/v1/integrations/{provider}/connect/start", {
          params: { path: { provider }, query: { write } },
        }),
      );
      before.current = signature(accounts);
      await openExternal(started.data.authorization_url);
      setWaiting(true);
    } catch (err) {
      setProblem(describe(err));
    }
  };
  return { waiting, cancel: () => setWaiting(false), problem, begin };
}

function ConnectButton(props: { provider: Provider; accounts: ExternalAccount[]; onConnected: () => Promise<void> }) {
  const label = LABEL[props.provider];
  const signIn = useSignIn(props.provider, props.accounts, props.onConnected);
  return (
    <div className="card">
      {signIn.waiting ? (
        <p>
          Finish signing in to {label} in your browser…{" "}
          <button className="quiet" onClick={signIn.cancel}>
            Cancel
          </button>
        </p>
      ) : (
        <button onClick={() => signIn.begin(false)}>
          {props.accounts.length ? `Connect another ${label} account` : `Connect ${label}`}
        </button>
      )}
      {signIn.problem && <p className="problem">{signIn.problem}</p>}
    </div>
  );
}

function AccountCard(props: { account: ExternalAccount; sources: CalendarSource[]; onChanged: () => Promise<void> }) {
  const { account } = props;
  const provider = account.provider as Provider;
  const label = LABEL[provider];
  const { run, error } = useAction(props.onChanged);
  const signIn = useSignIn(provider, [account], props.onChanged);
  const [disconnecting, setDisconnecting] = useState(false);
  return (
    <div className="card">
      <div className="list-head">
        <strong className="grow">{account.email}</strong>
        {!account.can_write && account.status === "active" && (
          <button className="quiet" onClick={() => signIn.begin(true)} title="Needed for two-way sync">
            {signIn.waiting ? "Waiting for the browser…" : "Allow editing"}
          </button>
        )}
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
        <p className="problem">
          {label} needs you to sign in again.{" "}
          <button className="quiet" onClick={() => signIn.begin(account.can_write)}>
            Sign in
          </button>
        </p>
      )}
      {signIn.problem && <p className="problem">{signIn.problem}</p>}
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
          {provider === "microsoft" && (
            <span className="muted">
              Microsoft has no sign-out for desktop apps: remove Agenda at account.live.com/consent/Manage too.
            </span>
          )}
        </div>
      )}
      <ul className="rows">
        {props.sources.map((source) => (
          <SourceRow key={source.id} source={source} canWrite={account.can_write} run={run} />
        ))}
      </ul>
      {error && <p className="problem">{error}</p>}
    </div>
  );
}

function SourceRow(props: {
  source: CalendarSource;
  canWrite: boolean;
  run: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const { source, run } = props;
  const { spaces } = useSpaces();
  const [problem, setProblem] = useState<string>();
  const label = LABEL[source.provider as Provider];
  const patch = (body: { space_id?: string | null; enabled?: boolean; sync_mode?: "read_only" | "two_way" }) =>
    run(async () => {
      setProblem(undefined);
      try {
        await call(
          (await api()).PATCH("/api/v1/calendar-sources/{source_id}", {
            params: { path: { source_id: source.id }, header: ifMatch(source.revision) },
            body,
          }),
        );
      } catch (err) {
        if (err instanceof ApiError && err.code === "WRITE_ACCESS_REQUIRED") {
          setProblem(`Use "Allow editing" above to let Agenda change ${label} events, then try again.`);
          return;
        }
        throw err;
      }
    });
  const writable = source.access_role === "owner" || source.access_role === "writer";
  return (
    <li className="row">
      <span className="dot" style={{ background: source.color }} />
      <span className="grow">
        {source.name}
        {source.is_primary && <span className="muted"> · primary</span>}
        <span className="meta">
          <SyncState source={source} />
        </span>
        {problem && <span className="problem"> {problem}</span>}
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
      {source.enabled && writable && (
        <label className="toggle" title={props.canWrite ? undefined : `Needs "Allow editing" first`}>
          <input
            type="checkbox"
            checked={source.sync_mode === "two_way"}
            onChange={(e) => patch({ sync_mode: e.target.checked ? "two_way" : "read_only" })}
          />{" "}
          Two-way
        </label>
      )}
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
  rate_limited: "the provider asked us to slow down; retrying",
  provider_unavailable: "the provider is unavailable; retrying",
  provider_unreachable: "offline; retrying",
  access_denied: "access denied",
  calendar_not_found: "calendar no longer exists",
  oauth_client_missing: "app registration missing",
  sign_in_rejected: "sign-in rejected; check the app registration",
};

function SyncState({ source }: { source: CalendarSource }) {
  if (!source.enabled) return <span>not imported</span>;
  if (source.sync_state === "error" || source.sync_state === "needs_reauth") {
    return <span className="overdue">{ERRORS[source.last_error ?? ""] ?? source.last_error ?? "sync failed"}</span>;
  }
  const extra = [
    source.sync_mode === "two_way" ? "two-way" : "read-only",
    source.pending_changes ? `${source.pending_changes} change(s) waiting to send` : "",
    source.open_conflicts ? `${source.open_conflicts} conflict(s)` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  if (!source.last_synced_at) return <span>waiting for first sync… · {extra}</span>;
  const at = new Date(source.last_synced_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return (
    <span>
      synced {at} · {extra}
    </span>
  );
}

// --------------------------------------------------------------------------- conflicts

type Version = NonNullable<SyncConflict["remote"]>;

function when(v: Version): string {
  if (v.all_day) return `${formatDay(v.start_date!)}, all day`;
  return `${formatDay(localDateOf(v.start_at!))} ${formatTime(v.start_at!)}–${formatTime(v.end_at!)}`;
}

function VersionView({ heading, version }: { heading: string; version: Version | null }) {
  return (
    <div className="grow">
      <strong>{heading}</strong>
      {!version || version.deleted ? (
        <p className="muted">Deleted</p>
      ) : (
        <>
          <p>{version.title}</p>
          <p className="muted">
            {when(version)}
            {version.location && <> · {version.location}</>}
            {version.rrule && <> · repeats</>}
          </p>
        </>
      )}
    </div>
  );
}

function Conflicts(props: { conflicts: SyncConflict[]; onChanged: () => Promise<void> }) {
  const { run, error } = useAction(async () => {
    await props.onChanged();
    notifyChanged();
  });
  const resolve = (id: string, resolution: "keep_local" | "keep_remote" | "keep_both") =>
    run(async () =>
      call(
        (await api()).POST("/api/v1/sync-conflicts/{conflict_id}/resolve", {
          params: { path: { conflict_id: id } },
          body: { resolution },
        }),
      ),
    );
  return (
    <div className="card stack">
      <h3>Changed in two places</h3>
      <p className="muted">
        These events changed both in Agenda and in the provider's calendar. Agenda kept both versions and won't overwrite
        either until you choose.
      </p>
      {props.conflicts.map((c) => {
        const label = LABEL[c.provider as Provider];
        return (
          <div key={c.id} className="stack">
            <span className="meta">{c.calendar_name}</span>
            <div className="form-grid">
              <VersionView heading="In Agenda" version={c.local} />
              <VersionView heading={`In ${label}`} version={c.remote ?? null} />
            </div>
            <div className="scope">
              <button onClick={() => resolve(c.id, "keep_local")}>Keep Agenda's</button>
              <button onClick={() => resolve(c.id, "keep_remote")}>Keep {label}'s</button>
              {c.can_keep_both && <button onClick={() => resolve(c.id, "keep_both")}>Keep both</button>}
            </div>
          </div>
        );
      })}
      {error && <p className="problem">{error}</p>}
    </div>
  );
}
