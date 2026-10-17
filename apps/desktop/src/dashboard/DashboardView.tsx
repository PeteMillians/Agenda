import type { Dashboard, WidgetPlacement } from "@agenda/contracts";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactGridLayout, { type Layout, useContainerWidth } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import { api } from "../api/backend";
import { ApiError, call, describe, ifMatch, notifyChanged } from "../api/call";
import { useSpaces } from "../state/spaces";
import { newPlacement, widgetRegistry } from "../widgets";
import { type Box, COLUMNS, type Direction, move, placeNew, ROW_HEIGHT, resize } from "./layout";
import { resolve } from "./registry";
import { WidgetFrame } from "./WidgetFrame";

const SAVE_DELAY_MS = 500;
/** Below this width a 12-column grid makes widgets unusably narrow, so they stack instead. */
const STACK_BELOW_PX = 700;

const sameGeometry = (a: readonly Box[], b: readonly Box[]) =>
  a.length === b.length &&
  a.every((box) => {
    const other = b.find((o) => o.i === box.i);
    return other && other.x === box.x && other.y === box.y && other.w === box.w && other.h === box.h;
  });

export function DashboardView(props: {
  dashboard: Dashboard;
  onSaved: (dashboard: Dashboard) => void;
  onReload: () => void;
  onDeleted: () => void;
}) {
  const [placements, setPlacements] = useState<WidgetPlacement[]>(props.dashboard.layout);
  const [editing, setEditing] = useState(false);
  const [problem, setProblem] = useState<string>();
  const revision = useRef(props.dashboard.revision);
  const pending = useRef<WidgetPlacement[] | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const saving = useRef(false);
  // measure first: rendering at the default 1280px for a frame makes the page flash wider
  const { width, containerRef, mounted } = useContainerWidth({ measureBeforeMount: true });

  // Persist the latest layout, one request at a time, a moment after the last change.
  const flush = async () => {
    if (saving.current || !pending.current) return;
    const layout = pending.current;
    pending.current = null;
    saving.current = true;
    try {
      const saved = await call(
        (await api()).PATCH("/api/v1/dashboard-profiles/{dashboard_id}", {
          params: { path: { dashboard_id: props.dashboard.id }, header: ifMatch(revision.current) },
          body: { layout },
        }),
      );
      revision.current = saved.data.revision;
      props.onSaved(saved.data);
      setProblem(undefined);
    } catch (e) {
      setProblem(
        e instanceof ApiError && e.code === "REVISION_CONFLICT"
          ? "This dashboard changed elsewhere and was reloaded."
          : `Layout not saved: ${describe(e)}`,
      );
      if (e instanceof ApiError && e.code === "REVISION_CONFLICT") props.onReload();
    } finally {
      saving.current = false;
      if (pending.current) void flush();
    }
  };
  const update = (next: WidgetPlacement[]) => {
    setPlacements(next);
    pending.current = next;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
  };
  useEffect(
    () => () => {
      clearTimeout(timer.current);
      void flush(); // don't lose a change made just before switching dashboards
    },
    [], // unmount only

  );

  const resolved = useMemo(() => placements.map((p) => ({ placement: p, resolved: resolve(p, widgetRegistry) })), [placements]);
  const boxes: Box[] = resolved.map(({ placement: p, resolved: r }) => ({
    i: p.key,
    x: p.x,
    y: p.y,
    w: p.w,
    h: p.h,
    minW: r.status === "ok" ? r.definition.layout.minW : 2,
    minH: r.status === "ok" ? r.definition.layout.minH : 2,
  }));

  const applyBoxes = (next: readonly Box[]) => {
    if (sameGeometry(next, boxes)) return; // the grid reports its layout on mount too
    update(
      placements.map((p) => {
        const box = next.find((b) => b.i === p.key);
        return box ? { ...p, x: box.x, y: box.y, w: box.w, h: box.h } : p;
      }),
    );
  };
  const setConfig = (key: string, config: unknown) =>
    update(
      placements.map((p) => {
        if (p.key !== key) return p;
        const r = resolve(p, widgetRegistry);
        // writing the current version records any migration that happened on load
        return { ...p, config: config as Record<string, unknown>, version: r.status === "ok" ? r.definition.version : p.version };
      }),
    );

  const stacked = mounted && width < STACK_BELOW_PX;
  const customizing = editing && !stacked;

  const renderWidget = (key: string) => {
    const { placement, resolved: r } = resolved.find((x) => x.placement.key === key)!;
    const frame = {
      editing: customizing,
      onMove: (d: Direction) => applyBoxes(move(boxes, placement.key, d)),
      onResize: (d: Direction) => applyBoxes(resize(boxes, placement.key, d)),
      onRemove: () => update(placements.filter((p) => p.key !== placement.key)),
    };
    if (r.status === "unknown") {
      return (
        <WidgetFrame title="Unavailable widget" {...frame}>
          <p className="muted">"{r.widgetId}" isn't available in this version of Agenda.</p>
        </WidgetFrame>
      );
    }
    const { definition: Widget, config } = r;
    const setOwnConfig = (c: unknown) => setConfig(placement.key, c);
    return (
      <WidgetFrame
        title={Widget.title}
        {...frame}
        settings={Widget.Settings && <Widget.Settings config={config} spaceIds={props.dashboard.space_ids} onChange={setOwnConfig} />}
      >
        <Widget.Component config={config} spaceIds={props.dashboard.space_ids} setConfig={setOwnConfig} />
      </WidgetFrame>
    );
  };

  return (
    <div className="dashboard">
      <header className="dashboard-head">
        <h2>{props.dashboard.name}</h2>
        {!stacked && (
          <button className={editing ? "" : "quiet"} aria-pressed={editing} onClick={() => setEditing((e) => !e)}>
            {editing ? "Done" : "Customize"}
          </button>
        )}
      </header>
      {customizing && (
        <DashboardSettings
          dashboard={props.dashboard}
          onAdd={(widgetId) => {
            const definition = widgetRegistry.find((w) => w.id === widgetId)!;
            update([...placements, newPlacement(definition, placeNew(boxes))]);
          }}
          onSaved={props.onReload}
          onDeleted={props.onDeleted}
          revision={revision}
        />
      )}
      {problem && <p className="problem">{problem}</p>}
      {props.dashboard.space_ids.length === 0 && (
        <p className="problem">This dashboard's spaces were removed. Choose spaces under Customize.</p>
      )}
      <div ref={containerRef}>
        {mounted && width < STACK_BELOW_PX && (
          <div className="stacked">
            {[...resolved]
              .sort((a, b) => a.placement.y - b.placement.y || a.placement.x - b.placement.x)
              .map(({ placement }) => (
                <div key={placement.key} style={{ height: placement.h * ROW_HEIGHT }}>
                  {renderWidget(placement.key)}
                </div>
              ))}
          </div>
        )}
        {mounted && width >= STACK_BELOW_PX && (
          <ReactGridLayout
            layout={boxes as Layout}
            width={width}
            gridConfig={{ cols: COLUMNS, rowHeight: ROW_HEIGHT, margin: [14, 14], containerPadding: [0, 0] }}
            dragConfig={{ enabled: customizing, handle: ".drag-handle", cancel: "button" }}
            resizeConfig={{ enabled: customizing }}
            onLayoutChange={(layout) => applyBoxes(layout as readonly Box[])}
          >
            {resolved.map(({ placement }) => (
              <div key={placement.key}>{renderWidget(placement.key)}</div>
            ))}
          </ReactGridLayout>
        )}
      </div>
      {placements.length === 0 && !editing && <p className="muted">This dashboard is empty. Choose Customize to add widgets.</p>}
    </div>
  );
}

function DashboardSettings(props: {
  dashboard: Dashboard;
  revision: React.MutableRefObject<number>;
  onAdd: (widgetId: string) => void;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const { spaces } = useSpaces();
  const [name, setName] = useState(props.dashboard.name);
  const [chosen, setChosen] = useState<string[]>(props.dashboard.space_ids);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [problem, setProblem] = useState<string>();
  const changed = name !== props.dashboard.name || chosen.join() !== props.dashboard.space_ids.join();

  const save = async () => {
    try {
      const saved = await call(
        (await api()).PATCH("/api/v1/dashboard-profiles/{dashboard_id}", {
          params: { path: { dashboard_id: props.dashboard.id }, header: ifMatch(props.revision.current) },
          body: { name, space_ids: chosen },
        }),
      );
      props.revision.current = saved.data.revision;
      props.onSaved();
      notifyChanged();
    } catch (e) {
      setProblem(describe(e));
    }
  };

  return (
    <div className="card customize">
      <div className="form-grid">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <fieldset className="spaces-choice">
          <legend>Spaces shown</legend>
          {spaces.map((space) => (
            <label key={space.id} className="toggle">
              <input
                type="checkbox"
                checked={chosen.includes(space.id)}
                onChange={(e) =>
                  setChosen(e.target.checked ? [...chosen, space.id] : chosen.filter((id) => id !== space.id))
                }
              />
              <span className="dot" style={{ background: space.color }} />
              {space.name}
            </label>
          ))}
        </fieldset>
        <button disabled={!changed || !name.trim() || chosen.length === 0} onClick={() => void save()}>
          Save
        </button>
      </div>
      <div className="add-widgets">
        <span className="muted">Add a widget:</span>
        {widgetRegistry.map((w) => (
          <button key={w.id} className="pill" title={w.description} onClick={() => props.onAdd(w.id)}>
            + {w.title}
          </button>
        ))}
      </div>
      <p className="muted">
        Drag a widget by its title bar and resize it from the corner. With the keyboard, focus ⠿ and use the arrow
        keys to move or Shift + arrow keys to resize.
      </p>
      {confirmDelete ? (
        <span className="scope">
          Delete this dashboard? Your tasks, events, and lists are not affected.
          <button
            onClick={async () => {
              try {
                await call(
                  (await api()).DELETE("/api/v1/dashboard-profiles/{dashboard_id}", {
                    params: { path: { dashboard_id: props.dashboard.id }, header: ifMatch(props.revision.current) },
                  }),
                );
                props.onDeleted();
              } catch (e) {
                setProblem(describe(e));
              }
            }}
          >
            Delete dashboard
          </button>
          <button className="quiet" onClick={() => setConfirmDelete(false)}>
            Cancel
          </button>
        </span>
      ) : (
        <button className="quiet danger" onClick={() => setConfirmDelete(true)}>
          Delete dashboard…
        </button>
      )}
      {problem && <p className="problem">{problem}</p>}
    </div>
  );
}
