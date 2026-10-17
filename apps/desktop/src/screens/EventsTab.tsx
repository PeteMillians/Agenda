import type { Occurrence } from "@agenda/contracts";
import { useState } from "react";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { addDays, formatDay, formatTime, localDateOf, localToIso, today, weekdayCode } from "../dates";

const RANGE_DAYS = 28;
type Scope = "this" | "following" | "all";
type Frequency = "" | "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";

function dayOf(o: Occurrence): string {
  return o.start_date ?? localDateOf(o.start_at!);
}

export function EventsTab({ spaceId }: { spaceId: string }) {
  const [from, setFrom] = useState(today());
  const to = addDays(from, RANGE_DAYS);
  const events = useLoad(
    async () =>
      (
        await call(
          (await api()).GET("/api/v1/spaces/{space_id}/events", {
            params: { path: { space_id: spaceId }, query: { from, to } },
          }),
        )
      ).data,
    [spaceId, from],
  );
  const { run, error } = useAction(events.reload);

  const target = (o: Occurrence, scope?: Scope) => ({
    path: { event_id: o.event_id },
    header: ifMatch(o.revision),
    query: o.is_recurring ? { scope, occurrence_start: o.occurrence_start } : {},
  });
  const rename = (o: Occurrence, title: string, scope?: Scope) =>
    run(async () => call((await api()).PATCH("/api/v1/events/{event_id}", { params: target(o, scope), body: { title } })));
  const remove = (o: Occurrence, scope?: Scope) =>
    run(async () => call((await api()).DELETE("/api/v1/events/{event_id}", { params: target(o, scope) })));

  const byDay = new Map<string, Occurrence[]>();
  for (const o of events.data ?? []) byDay.set(dayOf(o), [...(byDay.get(dayOf(o)) ?? []), o]);

  return (
    <section>
      <NewEvent spaceId={spaceId} onCreated={events.reload} />
      <div className="range">
        <button className="quiet" onClick={() => setFrom(addDays(from, -RANGE_DAYS))}>
          ← Earlier
        </button>
        <span>
          {formatDay(from)} – {formatDay(addDays(to, -1))}
        </span>
        <button className="quiet" onClick={() => setFrom(addDays(from, RANGE_DAYS))}>
          Later →
        </button>
        {from !== today() && (
          <button className="quiet" onClick={() => setFrom(today())}>
            Today
          </button>
        )}
      </div>
      {(error || events.error) && <p className="problem">{error ?? events.error}</p>}
      {[...byDay.entries()].map(([day, items]) => (
        <div key={day} className="day">
          <h3>{formatDay(day)}</h3>
          <ul className="rows">
            {items.map((o) => (
              <EventRow key={`${o.event_id}|${o.occurrence_start}`} occurrence={o} onRename={rename} onDelete={remove} />
            ))}
          </ul>
        </div>
      ))}
      {events.data?.length === 0 && <p className="muted">Nothing scheduled in these four weeks.</p>}
    </section>
  );
}

function EventRow(props: {
  occurrence: Occurrence;
  onRename: (o: Occurrence, title: string, scope?: Scope) => void;
  onDelete: (o: Occurrence, scope?: Scope) => void;
}) {
  const o = props.occurrence;
  const [mode, setMode] = useState<"idle" | "rename" | "delete">("idle");
  const [title, setTitle] = useState(o.title);

  const finish = (scope?: Scope) => {
    if (mode === "rename" && title.trim()) props.onRename(o, title, scope);
    if (mode === "delete") props.onDelete(o, scope);
    setMode("idle");
  };

  return (
    <li className="row">
      <span className="time">{o.all_day ? "All day" : `${formatTime(o.start_at!)}–${formatTime(o.end_at!)}`}</span>
      <div className="grow">
        {mode === "rename" ? (
          <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        ) : (
          <span>{o.title}</span>
        )}
        <div className="meta">
          {o.location && <span>{o.location}</span>}
          {o.is_recurring && <span title={o.rrule ?? ""}>↻ repeats</span>}
          {o.is_exception && <span>edited occurrence</span>}
        </div>
      </div>
      {mode === "idle" ? (
        <>
          <button className="quiet" onClick={() => setMode("rename")}>
            Rename
          </button>
          <button className="quiet" onClick={() => setMode("delete")}>
            Delete
          </button>
        </>
      ) : o.is_recurring ? (
        <span className="scope">
          {mode === "delete" ? "Delete" : "Rename"}:
          <button onClick={() => finish("this")}>This event</button>
          <button onClick={() => finish("following")}>This and following</button>
          <button onClick={() => finish("all")}>All events</button>
          <button className="quiet" onClick={() => setMode("idle")}>
            Cancel
          </button>
        </span>
      ) : (
        <span className="scope">
          <button onClick={() => finish()}>{mode === "delete" ? "Confirm delete" : "Save"}</button>
          <button className="quiet" onClick={() => setMode("idle")}>
            Cancel
          </button>
        </span>
      )}
    </li>
  );
}

function NewEvent(props: { spaceId: string; onCreated: () => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [day, setDay] = useState(today());
  const [allDay, setAllDay] = useState(false);
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("10:00");
  const [location, setLocation] = useState("");
  const [frequency, setFrequency] = useState<Frequency>("");
  const { run, error } = useAction(props.onCreated);

  const rrule = !frequency ? null : frequency === "WEEKLY" ? `FREQ=WEEKLY;BYDAY=${weekdayCode(day)}` : `FREQ=${frequency}`;
  const timing = allDay
    ? { all_day: true, start_date: day, end_date: addDays(day, 1) }
    : { start_at: localToIso(day, start), end_at: localToIso(day, end) };

  return (
    <form
      className="card form-grid"
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          await call(
            (await api()).POST("/api/v1/spaces/{space_id}/events", {
              params: { path: { space_id: props.spaceId } },
              body: { title, location, rrule, ...timing },
            }),
          );
          setTitle("");
          setLocation("");
        });
      }}
    >
      <input className="wide" placeholder="New event" value={title} onChange={(e) => setTitle(e.target.value)} />
      <label>
        Date
        <input type="date" value={day} onChange={(e) => setDay(e.target.value)} />
      </label>
      <label className="toggle">
        <input type="checkbox" checked={allDay} onChange={(e) => setAllDay(e.target.checked)} /> All day
      </label>
      {!allDay && (
        <>
          <label>
            From
            <input type="time" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>
            To
            <input type="time" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
        </>
      )}
      <label>
        Repeats
        <select value={frequency} onChange={(e) => setFrequency(e.target.value as Frequency)}>
          <option value="">Doesn't repeat</option>
          <option value="DAILY">Daily</option>
          <option value="WEEKLY">Weekly on {weekdayCode(day)}</option>
          <option value="MONTHLY">Monthly</option>
          <option value="YEARLY">Yearly</option>
        </select>
      </label>
      <input placeholder="Location (optional)" value={location} onChange={(e) => setLocation(e.target.value)} />
      <button type="submit" disabled={!title.trim()}>
        Add
      </button>
      {error && <p className="problem wide">{error}</p>}
    </form>
  );
}
