import type { CalendarSource, ExternalAccount, OAuthClientStatus, SyncConflict } from "@agenda/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, openExternal } from "../api/backend";
import { ApiError, call, describe, ifMatch, notifyChanged, useAction, useLoad } from "../api/call";
import { describeRule, formatDay, formatTime, localDateOf } from "../dates";
import { PROVIDER_NAMES, type Provider } from "../providers";
import { useSpaces } from "../state/spaces";

const SIGN_IN_TIMEOUT_MS = 5 * 60 * 1000;
const PRODUCT: Record<Provider, string> = { google: "Google Calendar", microsoft: "Outlook.com calendar" };

/** Provider accounts, which space each calendar feeds, two-way editing, and sync conflicts
 * (spec §07, §10). */
export function CalendarsPanel() {
  const accounts = useLoad(async () => (await call((await api()).GET("/api/v1/external-accounts"))).data, []);
  const sources = useLoad(async () => (await call((await api()).GET("/api/v1/calendar-sources"))).data, []);
  const conflicts = useLoad(async () => (await call((await api()).GET("/api/v1/sync-conflicts"))).data, []);
  const reload = useCallback(async () => {
    await accounts.reload();
    await sources.reload();
    await conflicts.reload();
  }, [accounts, sources, conflicts]);

  return (
    <div className="page-wide">
      <h2>Calendars</h2>
      <p className="muted">
        Agenda shows your Google and Outlook.com calendars in the space you choose and checks for changes every few
        minutes while it's running. Turn on <strong>Edit here</strong> for a calendar to change its events from Agenda
        too; edits made while offline are sent once you're back.
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

// --------------------------------------------------------------------------- providers

function ProviderSection(props: {
  provider: Provider;
  accounts: ExternalAccount[];
  sources: CalendarSource[];
  onChanged: () => Promise<void>;
}) {
  const { provider } = props;
  const client = useLoad(
    async () =>
      (await call((await api()).GET("/api/v1/integrations/{provider}/client", { params: { path: { provider } } }))).data,
    [provider],
  );
  const [editingClient, setEditingClient] = useState(false);
  const signIn = useSignIn(props.onChanged);
  if (!client.data) return client.error ? <p className="problem">{client.error}</p> : null;
  const status = client.data;

  return (
    <section className="card">
      <h3 className="section-title">{PRODUCT[provider]}</h3>
      {!status.vault && (
        <p className="problem">
          This computer has no credential vault Agenda can use, so accounts can't be connected. Agenda never stores
          sign-in tokens unprotected.
        </p>
      )}
      {status.vault?.includes("NOT a secure") && (
        <p className="muted">Development build: tokens are kept in a plain file in the dev data folder.</p>
      )}
      {!status.configured || editingClient ? (
        <ClientSetup
          provider={provider}
          status={status}
          onSaved={async () => {
            setEditingClient(false);
            await client.reload();
          }}
          onCancel={status.configured ? () => setEditingClient(false) : undefined}
        />
      ) : (
        <>
          {props.accounts.map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              sources={props.sources.filter((s) => s.external_account_id === account.id)}
              signIn={signIn}
              onChanged={props.onChanged}
            />
          ))}
          <SignInButton
            provider={provider}
            signIn={signIn}
            label={props.accounts.length ? "Connect another account" : `Connect ${PRODUCT[provider]}`}
          />
          <p className="muted">
            {provider === "google" ? "Google OAuth client" : "Azure application ID"}: <code>{status.client_id}</code>{" "}
            <button className="quiet" onClick={() => setEditingClient(true)}>
              Change
            </button>
          </p>
        </>
      )}
    </section>
  );
}

function ClientSetup(props: {
  provider: Provider;
  status: OAuthClientStatus;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const { provider } = props;
  const [clientId, setClientId] = useState(props.status.client_id ?? "");
  const [secret, setSecret] = useState("");
  const [problem, setProblem] = useState<string>();
  const needsSecret = provider === "google";
  return (
    <form
      className="stack"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await call(
            (await api()).PUT("/api/v1/integrations/{provider}/client", {
              params: { path: { provider } },
              body: { client_id: clientId, client_secret: needsSecret ? secret : null },
            }),
          );
          props.onSaved();
        } catch (err) {
          setProblem(describe(err));
        }
      }}
    >
      <strong>One-time setup</strong>
      {provider === "google" ? (
        <>
          <ol className="muted steps">
            <li>
              In Google Cloud Console, create a project and enable the <strong>Google Calendar API</strong>.
            </li>
            <li>
              Under <strong>OAuth consent screen</strong>, choose External, leave it in <strong>Testing</strong>, and
              add your Google address as a test user.
            </li>
            <li>
              Under <strong>Credentials</strong>, create an OAuth client ID of type <strong>Desktop app</strong>, then
              paste its ID and secret here. Google treats a desktop app's secret as non-confidential.
            </li>
          </ol>
          <p className="muted">
            In Testing mode Google asks you to sign in again every 7 days. Agenda will tell you when that's needed.
          </p>
        </>
      ) : (
        <ol className="muted steps">
          <li>
            In the Azure portal, open <strong>App registrations</strong> and choose <strong>New registration</strong>.
          </li>
          <li>
            Under supported account types, choose <strong>Personal Microsoft accounts only</strong>.
          </li>
          <li>
            Add a redirect URI of platform <strong>Public client/native (mobile &amp; desktop)</strong>:{" "}
            <code>{props.status.redirect_uri}</code>
          </li>
          <li>
            Copy the <strong>Application (client) ID</strong> from the overview page and paste it here. There's no
            secret to create.
          </li>
        </ol>
      )}
      <label>
        {provider === "google" ? "Client ID" : "Application (client) ID"}
        <input
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder={provider === "google" ? "…apps.googleusercontent.com" : "00000000-0000-0000-0000-000000000000"}
        />
      </label>
      {needsSecret && (
        <label>
          Client secret
          <input value={secret} onChange={(e) => setSecret(e.target.value)} type="password" autoComplete="off" />
        </label>
      )}
      <div className="form-grid">
        <button type="submit" disabled={!clientId.trim() || (needsSecret && !secret.trim())}>
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

// --------------------------------------------------------------------------- sign-in

type SignIn = ReturnType<typeof useSignIn>;

/** Opens the provider's sign-in in the system browser, then watches for the account to appear or
 * change (a new account, or new permissions on an existing one). */
function useSignIn(onSignedIn: () => Promise<void>) {
  const [waiting, setWaiting] = useState<Provider | null>(null);
  const [problem, setProblem] = useState<string>();
  const before = useRef("");
  const after = useRef<(() => Promise<void>) | null>(null);
  const signature = (list: ExternalAccount[]) => list.map((a) => `${a.id}:${a.revision}`).join();

  useEffect(() => {
    if (!waiting) return;
    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const now = (await call((await api()).GET("/api/v1/external-accounts"))).data;
        if (signature(now) !== before.current) {
          setWaiting(null);
          await onSignedIn();
          await after.current?.();
        } else if (Date.now() - started > SIGN_IN_TIMEOUT_MS) {
          setWaiting(null);
          setProblem("Sign-in timed out. Try again.");
        }
      } catch {
        // keep waiting; the backend may be restarting
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [waiting, onSignedIn]);

  const start = async (provider: Provider, { write = false, then }: { write?: boolean; then?: () => Promise<void> } = {}) => {
    setProblem(undefined);
    try {
      const client = await api();
      before.current = signature((await call(client.GET("/api/v1/external-accounts"))).data);
      const started = await call(
        client.POST("/api/v1/integrations/{provider}/connect/start", {
          params: { path: { provider }, query: { write } },
        }),
      );
      after.current = then ?? null;
      await openExternal(started.data.authorization_url);
      setWaiting(provider);
    } catch (err) {
      setProblem(describe(err));
    }
  };
  return { waiting, problem, start, cancel: () => setWaiting(null) };
}

function SignInButton(props: { provider: Provider; signIn: SignIn; label: string }) {
  const { signIn, provider } = props;
  return (
    <div>
      {signIn.waiting === provider ? (
        <p>
          Finish signing in to {PROVIDER_NAMES[provider]} in your browser…{" "}
          <button className="quiet" onClick={signIn.cancel}>
            Cancel
          </button>
        </p>
      ) : (
        <button onClick={() => signIn.start(provider)}>{props.label}</button>
      )}
      {signIn.problem && <p className="problem">{signIn.problem}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- accounts and calendars

function AccountCard(props: {
  account: ExternalAccount;
  sources: CalendarSource[];
  signIn: SignIn;
  onChanged: () => Promise<void>;
}) {
  const { account } = props;
  const provider = account.provider as Provider;
  const { run, error } = useAction(props.onChanged);
  const [disconnecting, setDisconnecting] = useState(false);
  return (
    <div className="account">
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
        <p className="problem">
          {PROVIDER_NAMES[provider]} needs you to sign in again.{" "}
          <button className="quiet" onClick={() => props.signIn.start(provider, { write: props.sources.some((s) => s.can_write) })}>
            Sign in again
          </button>
        </p>
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
          <SourceRow key={source.id} source={source} run={run} signIn={props.signIn} />
        ))}
      </ul>
      {error && <p className="problem">{error}</p>}
    </div>
  );
}

type SourcePatch = { space_id?: string | null; enabled?: boolean; sync_mode?: "read_only" | "two_way" };

function SourceRow(props: { source: CalendarSource; run: (action: () => Promise<unknown>) => Promise<void>; signIn: SignIn }) {
  const { source, run } = props;
  const { spaces } = useSpaces();
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const send = async (body: SourcePatch, revision = source.revision) =>
    call(
      (await api()).PATCH("/api/v1/calendar-sources/{source_id}", {
        params: { path: { source_id: source.id }, header: ifMatch(revision) },
        body,
      }),
    );
  const patch = (body: SourcePatch) => run(() => send(body));
  const writable = source.access_role === "owner" || source.access_role === "writer";
  const twoWay = source.sync_mode === "two_way";

  const setTwoWay = (on: boolean) => {
    if (!on) {
      if (source.pending_changes > 0) setConfirmDiscard(true);
      else void patch({ sync_mode: "read_only" });
      return;
    }
    void run(async () => {
      try {
        await send({ sync_mode: "two_way" });
      } catch (err) {
        if (!(err instanceof ApiError && err.code === "WRITE_ACCESS_REQUIRED")) throw err;
        // ask the provider for edit permission, then try again once the account has it
        await props.signIn.start(source.provider as Provider, {
          write: true,
          then: async () => {
            const fresh = (await call((await api()).GET("/api/v1/calendar-sources"))).data.find((s) => s.id === source.id);
            if (fresh?.can_write) await send({ sync_mode: "two_way" }, fresh.revision);
            notifyChanged();
          },
        });
      }
    });
  };

  return (
    <li className="row">
      <span className="dot" style={{ background: source.color }} />
      <span className="grow">
        {source.name}
        {source.is_primary && <span className="muted"> · primary</span>}
        <span className="meta">
          <SyncState source={source} />
          {source.pending_changes > 0 && (
            <span>
              {source.pending_changes} change{source.pending_changes === 1 ? "" : "s"} waiting to send
            </span>
          )}
          {source.open_conflicts > 0 && (
            <span className="overdue">
              {source.open_conflicts} conflict{source.open_conflicts === 1 ? "" : "s"}
            </span>
          )}
        </span>
        {confirmDiscard && (
          <span className="scope">
            Stop editing here and discard {source.pending_changes} unsent change{source.pending_changes === 1 ? "" : "s"}?
            <button
              onClick={() => {
                setConfirmDiscard(false);
                void patch({ sync_mode: "read_only" });
              }}
            >
              Discard
            </button>
            <button className="quiet" onClick={() => setConfirmDiscard(false)}>
              Cancel
            </button>
          </span>
        )}
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
        <label className="toggle" title="Change this calendar's events from Agenda">
          <input type="checkbox" checked={twoWay} onChange={(e) => setTwoWay(e.target.checked)} /> Edit here
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
  rate_limited: "asked us to slow down; retrying",
  provider_unavailable: "service unavailable; retrying",
  provider_unreachable: "offline; retrying",
  access_denied: "access denied",
  calendar_not_found: "calendar no longer exists",
  oauth_client_missing: "OAuth client missing",
  write_access_required: "needs permission to edit; turn Edit here off and on",
  unsupported_change: "a change can't be stored there",
};

function SyncState({ source }: { source: CalendarSource }) {
  if (!source.enabled) return <span>not imported</span>;
  if (source.sync_state === "error" || source.sync_state === "needs_reauth") {
    return <span className="overdue">{ERRORS[source.last_error ?? ""] ?? source.last_error ?? "sync failed"}</span>;
  }
  if (!source.last_synced_at) return <span>waiting for first sync…</span>;
  return <span>synced {new Date(source.last_synced_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>;
}

// --------------------------------------------------------------------------- conflicts

type Snapshot = Record<string, unknown>;

/** The fields a person would recognise, in order. A deletion is one line. */
function describeSnapshot(s: Snapshot): [string, string][] {
  if (s.deleted) return [["", "Deleted"]];
  if (s.status === "cancelled") return [["", "This occurrence is cancelled"]];
  const start = s.start_at as string | null;
  const end = s.end_at as string | null;
  const when = start
    ? `${formatDay(localDateOf(start))}, ${formatTime(start)}–${formatTime(end!)}`
    : `${formatDay(s.start_date as string)}, all day`;
  const rows: [string, string][] = [
    ["Title", String(s.title ?? "")],
    ["When", when],
  ];
  if (s.location) rows.push(["Where", String(s.location)]);
  if (s.description) rows.push(["Notes", String(s.description)]);
  if (s.rrule) rows.push(["Repeats", describeRule(String(s.rrule))]);
  return rows;
}

function Conflicts(props: { conflicts: SyncConflict[]; onChanged: () => Promise<void> }) {
  const { run, error } = useAction(props.onChanged);
  return (
    <section className="card conflicts">
      <h3 className="section-title">Needs your decision</h3>
      <p className="muted">
        These events changed both in Agenda and in their calendar before the two could sync. Nothing was overwritten;
        choose which version to keep.
      </p>
      {props.conflicts.map((c) => {
        const theirs = PROVIDER_NAMES[c.provider as Provider];
        const local = describeSnapshot(c.local);
        const remote = describeSnapshot(c.remote);
        const labels = [...new Set([...local, ...remote].map(([label]) => label))];
        const value = (rows: [string, string][], label: string) => rows.find(([l]) => l === label)?.[1] ?? "—";
        const choose = (resolution: SyncConflict["resolution"]) =>
          run(async () =>
            call(
              (await api()).POST("/api/v1/sync-conflicts/{conflict_id}/resolve", {
                params: { path: { conflict_id: c.id } },
                body: { resolution: resolution! },
              }),
            ),
          );
        return (
          <div key={c.id} className="conflict">
            <div className="list-head">
              <span className="dot" style={{ background: c.calendar_color }} />
              <strong className="grow">
                {c.title}
                {c.kind === "occurrence" && <span className="muted"> · one occurrence</span>}
                {c.kind === "series" && <span className="muted"> · repeating</span>}
              </strong>
              <span className="muted">{c.calendar_name}</span>
            </div>
            <table className="compare">
              <thead>
                <tr>
                  <th />
                  <th>In Agenda</th>
                  <th>In {theirs}</th>
                </tr>
              </thead>
              <tbody>
                {labels.map((label) => {
                  const mine = value(local, label);
                  const other = value(remote, label);
                  return (
                    <tr key={label} className={mine === other ? "same" : "differs"}>
                      <th>{label}</th>
                      <td>{mine}</td>
                      <td>{other}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="scope">
              <button onClick={() => choose("keep_local")}>Keep Agenda's</button>
              <button onClick={() => choose("keep_remote")}>Keep {theirs}'s</button>
              {c.choices.includes("keep_both") && (
                <button className="quiet" onClick={() => choose("keep_both")}>
                  Keep both
                </button>
              )}
            </div>
          </div>
        );
      })}
      {error && <p className="problem">{error}</p>}
    </section>
  );
}
