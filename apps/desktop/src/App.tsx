import type { Dashboard } from "@agenda/contracts";
import { useEffect, useRef, useState } from "react";
import { api, type BackendStatus, watchBackend } from "./api/backend";
import { call, describe, useLoad } from "./api/call";
import { DashboardView } from "./dashboard/DashboardView";
import { Setup } from "./screens/Setup";
import { SpacesPanel } from "./screens/SpacesPanel";
import { SpacesProvider, useSpaces } from "./state/spaces";
import { defaultLayout } from "./widgets";
import "./App.css";

const STATUS_TEXT: Record<BackendStatus["state"], string> = {
  starting: "Starting…",
  ready: "Connected",
  restarting: "Backend restarting…",
  failed: "Backend failed to start. Quit Agenda from the tray and open it again.",
};

export default function App() {
  const [status, setStatus] = useState<BackendStatus>({ state: "starting" });
  useEffect(() => watchBackend(setStatus), []);

  if (status.state !== "ready") {
    return (
      <main className="splash">
        <h1>Agenda</h1>
        <p className={`status status-${status.state}`} role="status">
          {STATUS_TEXT[status.state]}
          {status.state === "restarting" && ` (attempt ${status.attempt})`}
        </p>
      </main>
    );
  }
  return <Ready />;
}

function Ready() {
  const setup = useLoad(async () => (await call((await api()).GET("/api/v1/setup/status"))).data.completed, []);
  if (setup.error) return <p className="problem page">{setup.error}</p>;
  if (setup.data === undefined) return null;
  if (!setup.data) return <Setup onDone={setup.reload} />;
  return (
    <SpacesProvider>
      <Home />
    </SpacesProvider>
  );
}

function remembered(): string | null {
  try {
    return localStorage.getItem("agenda.dashboard");
  } catch {
    return null;
  }
}

function remember(id: string) {
  try {
    localStorage.setItem("agenda.dashboard", id);
  } catch {
    // per-viewer convenience only
  }
}

function Home() {
  const { spaces } = useSpaces();
  // Not live: a refetch in the middle of a drag would fight the grid.
  const dashboards = useLoad(async () => (await call((await api()).GET("/api/v1/dashboard-profiles"))).data, [], {
    live: false,
  });
  const [selected, setSelected] = useState<string | null>(remembered);
  const [view, setView] = useState<"dashboard" | "spaces">("dashboard");
  const [newName, setNewName] = useState("");
  const [problem, setProblem] = useState<string>();
  const [generation, setGeneration] = useState(0); // bumped to remount a dashboard with server state
  const seeding = useRef(false);

  // First run (or upgrading from Phase 1): one dashboard per space, so nothing looks empty.
  useEffect(() => {
    if (!dashboards.data || dashboards.data.length > 0 || spaces.length === 0 || seeding.current) return;
    seeding.current = true;
    void (async () => {
      try {
        const client = await api();
        for (const space of spaces) {
          await call(
            client.POST("/api/v1/dashboard-profiles", {
              body: { name: space.name, space_ids: [space.id], layout: defaultLayout() },
            }),
          );
        }
        await dashboards.reload();
      } catch (e) {
        setProblem(describe(e));
      }
    })();
  }, [dashboards, spaces]);

  const list = dashboards.data ?? [];
  const current: Dashboard | undefined = list.find((d) => d.id === selected) ?? list[0];
  const open = (id: string) => {
    setSelected(id);
    setView("dashboard");
    remember(id);
  };

  return (
    <div className="home">
      <nav className="sidebar" aria-label="Dashboards">
        <h1>Agenda</h1>
        <ul>
          {list.map((d) => (
            <li key={d.id}>
              <button
                className={view === "dashboard" && d.id === current?.id ? "space active" : "space"}
                aria-current={view === "dashboard" && d.id === current?.id ? "page" : undefined}
                onClick={() => open(d.id)}
              >
                <span className="dots">
                  {d.space_ids.slice(0, 3).map((id) => (
                    <span key={id} className="dot" style={{ background: spaces.find((s) => s.id === id)?.color }} />
                  ))}
                </span>
                {d.name}
              </button>
            </li>
          ))}
        </ul>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              const created = await call(
                (await api()).POST("/api/v1/dashboard-profiles", {
                  body: { name: newName, space_ids: spaces.map((s) => s.id), layout: defaultLayout() },
                }),
              );
              setNewName("");
              await dashboards.reload();
              open(created.data.id);
            } catch (err) {
              setProblem(describe(err));
            }
          }}
        >
          <input placeholder="New dashboard" value={newName} onChange={(e) => setNewName(e.target.value)} />
        </form>
        <div className="sidebar-foot">
          <button className={view === "spaces" ? "space active" : "space"} onClick={() => setView("spaces")}>
            Spaces
          </button>
        </div>
        {(problem || dashboards.error) && <p className="problem">{problem ?? dashboards.error}</p>}
      </nav>
      <main className="content">
        {view === "spaces" ? (
          <SpacesPanel />
        ) : (
          current && (
            <DashboardView
              key={`${current.id}:${generation}`}
              dashboard={current}
              onSaved={(saved) => dashboards.setData(list.map((d) => (d.id === saved.id ? saved : d)))}
              onReload={async () => {
                await dashboards.reload();
                setGeneration((g) => g + 1);
              }}
              onDeleted={async () => {
                await dashboards.reload();
                setSelected(null);
              }}
            />
          )
        )}
      </main>
    </div>
  );
}
