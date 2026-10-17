import type { Occurrence } from "@agenda/contracts";
import { useState } from "react";
import { z } from "zod";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { defineWidget, type WidgetProps } from "../dashboard/registry";
import { addDays, describeRule, formatDay, formatTime, localDateOf, localToIso, today, weekdayCode } from "../dates";
import { SpaceDot, SpacePicker } from "../state/spaces";

type Scope = "this" | "following" | "all";
type Frequency = "" | "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";

const Config = z.object({ days: z.union([z.literal(7), z.literal(14), z.literal(28)]) });
type Config = z.infer<typeof Config>;

const dayOf = (o: Occurrence) => o.start_date ?? localDateOf(o.start_at!);
const timeOf = (iso: string) => {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

function EventsWidget({ config, spaceIds }: WidgetProps<Config>) {
  const [from, setFrom] = useState(today());
  const to = addDays(from, config.days);
  const events = useLoad(async () => {
    const client = await api();
    const all = await Promise.all(
      spaceIds.map((space_id) =>
        call(client.GET("/api/v1/spaces/{space_id}/events", { params: { path: { space_id }, query: { from, to } } })).then(
          (r) => r.data.map((o) => ({ ...o, spaceId: space_id })),
        ),
      ),
    );
    return all.flat().sort((a, b) => sortKey(a).localeCompare(sortKey(b)));
  }, [spaceIds.join(), from, to]);
  const { run, error } = useAction(events.reload);

  const params = (o: Occurrence, scope?: Scope) => ({
    path: { event_id: o.event_id },
    header: ifMatch(o.revision),
    query: o.is_recurring ? { scope, occurrence_start: o.occurrence_start } : {},
  });
  const save = (o: Occurrence, body: Record<string, unknown>, scope?: Scope) =>
    run(async () => call((await api()).PATCH("/api/v1/events/{event_id}", { params: params(o, scope), body })));
  const remove = (o: Occurrence, scope?: Scope) =>
    run(async () => call((await api()).DELETE("/api/v1/events/{event_id}", { params: params(o, scope) })));

  const byDay = new Map<string, (Occurrence & { spaceId: string })[]>();
  for (const o of events.data ?? []) byDay.set(dayOf(o), [...(byDay.get(dayOf(o)) ?? []), o]);

  return (
    <div className="widget-content">
      <NewEvent spaceIds={spaceIds} onDone={events.reload} />
      <div className="range">
        <button className="quiet" aria-label="Earlier" onClick={() => setFrom(addDays(from, -config.days))}>
          ←
        </button>
        <span>
          {formatDay(from)} – {formatDay(addDays(to, -1))}
        </span>
        <button className="quiet" aria-label="Later" onClick={() => setFrom(addDays(from, config.days))}>
          →
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
          <h4>{day === today() ? "Today" : formatDay(day)}</h4>
          <ul className="rows">
            {items.map((o) => (
              <EventRow
                key={`${o.event_id}|${o.occurrence_start}`}
                occurrence={o}
                spaceId={spaceIds.length > 1 ? o.spaceId : undefined}
                onSave={save}
                onDelete={remove}
              />
            ))}
          </ul>
        </div>
      ))}
      {events.data?.length === 0 && <p className="muted">Nothing scheduled.</p>}
    </div>
  );
}

function sortKey(o: Occurrence): string {
  return o.start_at ? new Date(o.start_at).toISOString() : `${o.start_date}T00:00:00.000Z`;
}

function EventRow(props: {
  occurrence: Occurrence;
  spaceId?: string;
  onSave: (o: Occurrence, body: Record<string, unknown>, scope?: Scope) => void;
  onDelete: (o: Occurrence, scope?: Scope) => void;
}) {
  const o = props.occurrence;
  const [mode, setMode] = useState<"idle" | "edit" | "delete">("idle");
  const [pending, setPending] = useState<Record<string, unknown> | null>(null);

  const finish = (scope?: Scope) => {
    if (mode === "delete") props.onDelete(o, scope);
    if (mode === "edit" && pending) props.onSave(o, pending, scope);
    setMode("idle");
    setPending(null);
  };
  // Recurring events ask which occurrences to change; one-off events apply immediately.
  const askScope = o.is_recurring && (mode === "delete" || pending !== null);

  return (
    <li className="row">
      <span className="time">{o.all_day ? "All day" : `${formatTime(o.start_at!)}–${formatTime(o.end_at!)}`}</span>
      <div className="grow">
        <span>{o.title}</span>
        <div className="meta">
          {props.spaceId && <SpaceDot spaceId={props.spaceId} />}
          {o.location && <span>{o.location}</span>}
          {o.rrule && <span title={o.rrule}>↻ {describeRule(o.rrule)}</span>}
          {o.is_exception && <span>edited occurrence</span>}
        </div>
        {mode === "edit" && pending === null && (
          <EventEditor
            occurrence={o}
            onSave={(body) => (o.is_recurring ? setPending(body) : (props.onSave(o, body), setMode("idle")))}
            onCancel={() => setMode("idle")}
          />
        )}
      </div>
      {mode === "idle" && (
        <>
          <button className="quiet" onClick={() => setMode("edit")}>
            Edit
          </button>
          <button className="quiet" onClick={() => setMode("delete")}>
            Delete
          </button>
        </>
      )}
      {askScope && (
        <span className="scope">
          {mode === "delete" ? "Delete" : "Change"}:
          <button onClick={() => finish("this")}>This event</button>
          <button onClick={() => finish("following")}>This and following</button>
          <button onClick={() => finish("all")}>All events</button>
          <button className="quiet" onClick={() => (setMode("idle"), setPending(null))}>
            Cancel
          </button>
        </span>
      )}
      {mode === "delete" && !o.is_recurring && (
        <span className="scope">
          <button onClick={() => finish()}>Confirm delete</button>
          <button className="quiet" onClick={() => setMode("idle")}>
            Cancel
          </button>
        </span>
      )}
    </li>
  );
}

function EventEditor(props: { occurrence: Occurrence; onSave: (body: Record<string, unknown>) => void; onCancel: () => void }) {
  const o = props.occurrence;
  const [title, setTitle] = useState(o.title);
  const [location, setLocation] = useState(o.location);
  const [day, setDay] = useState(dayOf(o));
  const [allDay, setAllDay] = useState(o.all_day);
  const [start, setStart] = useState(o.start_at ? timeOf(o.start_at) : "09:00");
  const [end, setEnd] = useState(o.end_at ? timeOf(o.end_at) : "10:00");

  return (
    <form
      className="editor form-grid"
      onSubmit={(e) => {
        e.preventDefault();
        const timing = allDay
          ? { all_day: true, start_date: day, end_date: o.all_day && day === dayOf(o) ? o.end_date : addDays(day, 1) }
          : { all_day: false, start_at: localToIso(day, start), end_at: localToIso(day, end) };
        props.onSave({ title, location, ...timing });
      }}
    >
      <label className="wide">
        Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </label>
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
      <label className="wide">
        Location
        <input value={location} onChange={(e) => setLocation(e.target.value)} />
      </label>
      <button type="submit" disabled={!title.trim()}>
        Save
      </button>
      <button type="button" className="quiet" onClick={props.onCancel}>
        Cancel
      </button>
    </form>
  );
}

function NewEvent(props: { spaceIds: string[]; onDone: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [space, setSpace] = useState(props.spaceIds[0]);
  const [day, setDay] = useState(today());
  const [allDay, setAllDay] = useState(false);
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("10:00");
  const [location, setLocation] = useState("");
  const [frequency, setFrequency] = useState<Frequency>("");
  const { run, error } = useAction(props.onDone);
  const spaceId = props.spaceIds.includes(space) ? space : props.spaceIds[0];

  const rrule = !frequency ? null : frequency === "WEEKLY" ? `FREQ=WEEKLY;BYDAY=${weekdayCode(day)}` : `FREQ=${frequency}`;
  const timing = allDay
    ? { all_day: true, start_date: day, end_date: addDays(day, 1) }
    : { start_at: localToIso(day, start), end_at: localToIso(day, end) };

  return (
    <form
      className="form-grid compose"
      // fold the extra fields away once focus leaves an empty form
      onBlur={(e) => !title && !e.currentTarget.contains(e.relatedTarget as Node | null) && setOpen(false)}
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          await call(
            (await api()).POST("/api/v1/spaces/{space_id}/events", {
              params: { path: { space_id: spaceId } },
              body: { title, location, rrule, ...timing },
            }),
          );
          setTitle("");
          setLocation("");
          setFrequency(""); // a leftover "weekly" would silently make the next event a series
        });
      }}
    >
      <input
        className="grow"
        placeholder="Add an event"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onFocus={() => setOpen(true)}
      />
      <button type="submit" disabled={!title.trim()}>
        Add
      </button>
      {open && (
        <div className="form-grid wide">
          <SpacePicker spaceIds={props.spaceIds} value={spaceId} onChange={setSpace} />
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
        </div>
      )}
      {error && <p className="problem wide">{error}</p>}
    </form>
  );
}

function EventsSettings({ config, onChange }: { config: Config; onChange: (c: Config) => void }) {
  return (
    <label>
      Show
      <select value={config.days} onChange={(e) => onChange({ days: Number(e.target.value) as Config["days"] })}>
        <option value={7}>One week</option>
        <option value={14}>Two weeks</option>
        <option value={28}>Four weeks</option>
      </select>
    </label>
  );
}

export const upcomingEvents = defineWidget<Config>({
  id: "agenda.upcoming-events",
  version: 1,
  title: "Upcoming events",
  description: "Your calendar for the next few weeks",
  configSchema: Config,
  defaultConfig: { days: 14 },
  layout: { w: 7, h: 9, minW: 4, minH: 4 },
  Component: EventsWidget,
  Settings: EventsSettings,
});
