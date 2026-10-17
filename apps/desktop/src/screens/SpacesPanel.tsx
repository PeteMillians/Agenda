import type { Space } from "@agenda/contracts";
import { useState } from "react";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { useSpaces } from "../state/spaces";

const PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#64748b"];
const TYPES: Space["type"][] = ["personal", "work", "family", "custom"];

/** Spaces own the data; dashboards only show them. */
export function SpacesPanel() {
  const shared = useSpaces();
  const all = useLoad(
    async () => (await call((await api()).GET("/api/v1/spaces", { params: { query: { include_archived: true } } }))).data,
    [],
  );
  const reload = async () => {
    await all.reload();
    await shared.reload();
  };
  const { run, error } = useAction(reload);
  const [name, setName] = useState("");
  const [color, setColor] = useState(PALETTE[5]);

  const patch = (space: Space, body: Record<string, unknown>) =>
    run(async () =>
      call(
        (await api()).PATCH("/api/v1/spaces/{space_id}", {
          params: { path: { space_id: space.id }, header: ifMatch(space.revision) },
          body,
        }),
      ),
    );

  return (
    <div className="page-wide">
      <h2>Spaces</h2>
      <p className="muted">
        A space holds its own events, tasks, chores, lists, and people. Dashboards choose which spaces to show.
      </p>
      <form
        className="card form-grid"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await call((await api()).POST("/api/v1/spaces", { body: { name, color, type: "custom" } }));
            setName("");
          });
        }}
      >
        <input className="grow" placeholder="New space name" value={name} onChange={(e) => setName(e.target.value)} />
        <ColorChoice value={color} onChange={setColor} />
        <button type="submit" disabled={!name.trim()}>
          Add space
        </button>
      </form>
      {(error || all.error) && <p className="problem">{error ?? all.error}</p>}
      <ul className="rows card">
        {(all.data ?? []).map((space) => (
          <SpaceRow key={space.id} space={space} onPatch={(body) => patch(space, body)} />
        ))}
      </ul>
    </div>
  );
}

function SpaceRow({ space, onPatch }: { space: Space; onPatch: (body: Record<string, unknown>) => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(space.name);
  const [color, setColor] = useState(space.color);
  const [type, setType] = useState(space.type);
  const archived = space.archived_at !== null;

  if (editing) {
    return (
      <li className="row">
        <form
          className="form-grid grow"
          onSubmit={(e) => {
            e.preventDefault();
            setEditing(false);
            onPatch({ name, color, type });
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          <select value={type} onChange={(e) => setType(e.target.value as Space["type"])} aria-label="Type">
            {TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <ColorChoice value={color} onChange={setColor} />
          <button type="submit" disabled={!name.trim()}>
            Save
          </button>
          <button type="button" className="quiet" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      </li>
    );
  }
  return (
    <li className="row">
      <span className="dot" style={{ background: space.color }} />
      <span className={archived ? "grow done" : "grow"}>
        {space.name} <span className="muted">· {space.type}</span>
      </span>
      {archived ? (
        <button className="quiet" onClick={() => onPatch({ archived: false })}>
          Restore
        </button>
      ) : (
        <>
          <button className="quiet" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button className="quiet" onClick={() => onPatch({ archived: true })}>
            Archive
          </button>
        </>
      )}
    </li>
  );
}

function ColorChoice({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  return (
    <span className="swatches" role="radiogroup" aria-label="Color">
      {PALETTE.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={c === value}
          aria-label={c}
          className={c === value ? "swatch chosen" : "swatch"}
          style={{ background: c }}
          onClick={() => onChange(c)}
        />
      ))}
    </span>
  );
}
