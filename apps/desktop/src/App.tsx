import type { Space } from "@agenda/contracts";
import { useEffect, useState } from "react";
import { api, type BackendStatus, watchBackend } from "./api/backend";
import { call, useAction, useLoad } from "./api/call";
import { Setup } from "./screens/Setup";
import { SpaceView } from "./screens/SpaceView";
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
  return <Home />;
}

function readSelected(): string | null {
  try {
    return localStorage.getItem("agenda.space");
  } catch {
    return null;
  }
}

function Home() {
  const spaces = useLoad(async () => (await call((await api()).GET("/api/v1/spaces"))).data, []);
  const [selected, setSelected] = useState<string | null>(readSelected);
  const [newName, setNewName] = useState("");
  const { run, error } = useAction(spaces.reload);

  const list = spaces.data ?? [];
  const current: Space | undefined = list.find((s) => s.id === selected) ?? list[0];

  function choose(id: string) {
    setSelected(id);
    try {
      localStorage.setItem("agenda.space", id);
    } catch {
      // per-viewer convenience only
    }
  }

  return (
    <div className="home">
      <nav className="sidebar" aria-label="Spaces">
        <h1>Agenda</h1>
        <ul>
          {list.map((space) => (
            <li key={space.id}>
              <button
                className={space.id === current?.id ? "space active" : "space"}
                onClick={() => choose(space.id)}
              >
                <span className="dot" style={{ background: space.color }} />
                {space.name}
              </button>
            </li>
          ))}
        </ul>
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await call((await api()).POST("/api/v1/spaces", { body: { name: newName } }));
              setNewName("");
            });
          }}
        >
          <input placeholder="New space" value={newName} onChange={(e) => setNewName(e.target.value)} />
          {newName.trim() && <button type="submit">Add space</button>}
        </form>
        {(error || spaces.error) && <p className="problem">{error ?? spaces.error}</p>}
      </nav>
      <main className="content">{current && <SpaceView key={current.id} space={current} />}</main>
    </div>
  );
}
