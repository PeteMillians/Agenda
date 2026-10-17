import type { Person, Repeat, Task } from "@agenda/contracts";
import { useState } from "react";
import { z } from "zod";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { defineWidget, type WidgetProps } from "../dashboard/registry";
import { describeRule, formatDay, formatTime, localDateOf, today, weekdayCode } from "../dates";
import { SpaceDot, SpacePicker } from "../state/spaces";

type RepeatChoice = "none" | "weekly" | "monthly" | "after" | "custom";

function toRepeat(choice: RepeatChoice, due: string, days: number, custom: string): Repeat {
  if (choice === "weekly") return { mode: "schedule", rrule: `FREQ=WEEKLY;BYDAY=${weekdayCode(due)}` };
  if (choice === "monthly") return { mode: "schedule", rrule: "FREQ=MONTHLY" };
  if (choice === "after") return { mode: "after_completion", interval_days: days };
  if (choice === "custom") return { mode: "schedule", rrule: custom };
  return { mode: "none" };
}

function fromRepeat(repeat: Repeat): { choice: RepeatChoice; days: number; custom: string } {
  if (repeat.mode === "after_completion") return { choice: "after", days: repeat.interval_days ?? 7, custom: "" };
  if (repeat.mode === "schedule") return { choice: "custom", days: 7, custom: repeat.rrule ?? "" };
  return { choice: "none", days: 7, custom: "" };
}

function describeRepeat(repeat: Repeat): string | null {
  if (repeat.mode === "after_completion") return `every ${repeat.interval_days} days after done`;
  if (repeat.mode === "schedule" && repeat.rrule) return describeRule(repeat.rrule);
  return null;
}

const dueDay = (t: Task) => t.due_date ?? (t.due_at ? localDateOf(t.due_at) : null);

const Config = z.object({
  show: z.enum(["all", "tasks", "chores"]),
  personId: z.string().nullable(),
  completed: z.boolean(),
});
type Config = z.infer<typeof Config>;

function TasksWidget({ config, spaceIds }: WidgetProps<Config>) {
  const tasks = useLoad(async () => {
    const client = await api();
    const status = config.completed ? "done" : "open";
    const pages = await Promise.all(
      spaceIds.map((space_id) =>
        call(client.GET("/api/v1/spaces/{space_id}/tasks", { params: { path: { space_id }, query: { status, limit: 200 } } })),
      ),
    );
    return pages.flatMap((p) => p.data).sort((a, b) => (dueDay(a) ?? "9999").localeCompare(dueDay(b) ?? "9999"));
  }, [spaceIds.join(), config.completed]);
  const people = usePeople(spaceIds);
  const { run, error } = useAction(tasks.reload);

  const shown = (tasks.data ?? []).filter(
    (t) =>
      (config.show === "all" || (config.show === "chores") === (t.repeat.mode !== "none")) &&
      (!config.personId || t.assignee_person_id === config.personId),
  );
  const patch = (task: Task, body: Record<string, unknown>) =>
    run(async () =>
      call(
        (await api()).PATCH("/api/v1/tasks/{task_id}", {
          params: { path: { task_id: task.id }, header: ifMatch(task.revision) },
          body,
        }),
      ),
    );

  return (
    <div className="widget-content">
      {!config.completed && <NewTask spaceIds={spaceIds} people={people} onDone={tasks.reload} />}
      {(error || tasks.error) && <p className="problem">{error ?? tasks.error}</p>}
      <ul className="rows">
        {shown.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            multiSpace={spaceIds.length > 1}
            people={people.filter((p) => p.space_id === task.space_id)}
            onPatch={(body) => patch(task, body)}
            onArchive={() =>
              run(async () =>
                call(
                  (await api()).DELETE("/api/v1/tasks/{task_id}", {
                    params: { path: { task_id: task.id }, header: ifMatch(task.revision) },
                  }),
                ),
              )
            }
          />
        ))}
      </ul>
      {tasks.data && shown.length === 0 && <p className="muted">{config.completed ? "Nothing completed." : "All clear."}</p>}
    </div>
  );
}

export function usePeople(spaceIds: string[]): Person[] {
  const people = useLoad(async () => {
    const client = await api();
    const lists = await Promise.all(
      spaceIds.map((space_id) => call(client.GET("/api/v1/spaces/{space_id}/people", { params: { path: { space_id } } }))),
    );
    return lists.flatMap((l) => l.data);
  }, [spaceIds.join()]);
  return people.data ?? [];
}

function TaskRow(props: {
  task: Task;
  multiSpace: boolean;
  people: Person[];
  onPatch: (body: Record<string, unknown>) => void;
  onArchive: () => void;
}) {
  const { task } = props;
  const [editing, setEditing] = useState(false);
  const day = dueDay(task);
  const overdue = task.status === "open" && day !== null && day < today();
  const repeat = describeRepeat(task.repeat);
  const assignee = props.people.find((p) => p.id === task.assignee_person_id);

  return (
    <li className="row">
      <input
        type="checkbox"
        aria-label={`Complete ${task.title}`}
        checked={task.status === "done"}
        onChange={() => props.onPatch({ status: task.status === "done" ? "open" : "done" })}
      />
      <div className="grow">
        <span className={task.status === "done" ? "done" : undefined}>{task.title}</span>
        <div className="meta">
          {props.multiSpace && <SpaceDot spaceId={task.space_id} />}
          {day && (
            <span className={overdue ? "overdue" : undefined}>
              {formatDay(day)}
              {task.due_at && ` ${formatTime(task.due_at)}`}
            </span>
          )}
          {assignee && <span className="chip">{assignee.display_name}</span>}
          {repeat && <span>↻ {repeat}</span>}
          {task.notes && <span title={task.notes}>✎ note</span>}
        </div>
        {editing && (
          <TaskEditor
            task={task}
            people={props.people}
            onSave={(body) => {
              setEditing(false);
              props.onPatch(body);
            }}
            onCancel={() => setEditing(false)}
          />
        )}
      </div>
      {!editing && (
        <>
          <button className="quiet" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button className="quiet" onClick={props.onArchive}>
            Archive
          </button>
        </>
      )}
    </li>
  );
}

function TaskEditor(props: {
  task: Task;
  people: Person[];
  onSave: (body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const { task } = props;
  const initial = fromRepeat(task.repeat);
  const [title, setTitle] = useState(task.title);
  const [notes, setNotes] = useState(task.notes);
  const [due, setDue] = useState(dueDay(task) ?? "");
  const [assignee, setAssignee] = useState(task.assignee_person_id ?? "");
  const [choice, setChoice] = useState<RepeatChoice>(initial.choice);
  const [days, setDays] = useState(initial.days);
  const [custom, setCustom] = useState(initial.custom || `FREQ=WEEKLY;BYDAY=${weekdayCode(due || today())}`);

  return (
    <form
      className="editor form-grid"
      onSubmit={(e) => {
        e.preventDefault();
        props.onSave({
          title,
          notes,
          ...(task.due_at && due === localDateOf(task.due_at) ? {} : { due_date: due || null }),
          assignee_person_id: assignee || null,
          repeat: toRepeat(choice, due, days, custom),
        });
      }}
    >
      <label className="wide">
        Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </label>
      <label>
        Due
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
      </label>
      <AssigneeSelect people={props.people} value={assignee} onChange={setAssignee} />
      <RepeatFields choice={choice} setChoice={setChoice} days={days} setDays={setDays} custom={custom} setCustom={setCustom} />
      <label className="wide">
        Notes
        <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
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

function AssigneeSelect(props: { people: Person[]; value: string; onChange: (id: string) => void }) {
  return (
    <label>
      For
      <select value={props.value} onChange={(e) => props.onChange(e.target.value)}>
        <option value="">Anyone</option>
        {props.people.map((p) => (
          <option key={p.id} value={p.id}>
            {p.display_name}
          </option>
        ))}
      </select>
    </label>
  );
}

function RepeatFields(props: {
  choice: RepeatChoice;
  setChoice: (c: RepeatChoice) => void;
  days: number;
  setDays: (d: number) => void;
  custom: string;
  setCustom: (c: string) => void;
}) {
  return (
    <>
      <label>
        Repeats
        <select value={props.choice} onChange={(e) => props.setChoice(e.target.value as RepeatChoice)}>
          <option value="none">Doesn't repeat</option>
          <option value="weekly">Weekly on the due day</option>
          <option value="monthly">Monthly on the due date</option>
          <option value="after">Some days after it's done</option>
          <option value="custom">Custom rule…</option>
        </select>
      </label>
      {props.choice === "after" && (
        <label>
          Days after done
          <input type="number" min={1} max={3650} value={props.days} onChange={(e) => props.setDays(Number(e.target.value))} />
        </label>
      )}
      {props.choice === "custom" && (
        <label className="wide">
          RRULE
          <input value={props.custom} onChange={(e) => props.setCustom(e.target.value)} />
        </label>
      )}
    </>
  );
}

function NewTask(props: { spaceIds: string[]; people: Person[]; onDone: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [space, setSpace] = useState(props.spaceIds[0]);
  const [due, setDue] = useState("");
  const [assignee, setAssignee] = useState("");
  const [choice, setChoice] = useState<RepeatChoice>("none");
  const [days, setDays] = useState(7);
  const [custom, setCustom] = useState("FREQ=WEEKLY;BYDAY=MO");
  const { run, error } = useAction(props.onDone);
  const spaceId = props.spaceIds.includes(space) ? space : props.spaceIds[0];
  const needsDue = choice === "weekly" || choice === "monthly" || choice === "custom";

  return (
    <form
      className="form-grid compose"
      // fold the extra fields away once focus leaves an empty form
      onBlur={(e) => !title && !e.currentTarget.contains(e.relatedTarget as Node | null) && setOpen(false)}
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          await call(
            (await api()).POST("/api/v1/spaces/{space_id}/tasks", {
              params: { path: { space_id: spaceId } },
              body: {
                title,
                due_date: due || null,
                assignee_person_id: assignee || null,
                repeat: toRepeat(choice, due, days, custom),
              },
            }),
          );
          setTitle("");
          setChoice("none"); // a leftover repeat would silently make the next task a chore
        });
      }}
    >
      <input
        className="grow"
        placeholder="Add a task or chore"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onFocus={() => setOpen(true)}
      />
      <button type="submit" disabled={!title.trim() || (needsDue && !due)}>
        Add
      </button>
      {open && (
        <div className="form-grid wide">
          <SpacePicker spaceIds={props.spaceIds} value={spaceId} onChange={setSpace} />
          <label>
            Due
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
          </label>
          <AssigneeSelect people={props.people.filter((p) => p.space_id === spaceId)} value={assignee} onChange={setAssignee} />
          <RepeatFields choice={choice} setChoice={setChoice} days={days} setDays={setDays} custom={custom} setCustom={setCustom} />
          {needsDue && !due && <p className="muted wide">Scheduled chores need a due date.</p>}
        </div>
      )}
      {error && <p className="problem wide">{error}</p>}
    </form>
  );
}

function TasksSettings({ config, onChange, spaceIds }: { config: Config; onChange: (c: Config) => void; spaceIds: string[] }) {
  const people = usePeople(spaceIds);
  return (
    <div className="form-grid">
      <label>
        Whose
        <select value={config.personId ?? ""} onChange={(e) => onChange({ ...config, personId: e.target.value || null })}>
          <option value="">Everyone's</option>
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.display_name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Show
        <select value={config.show} onChange={(e) => onChange({ ...config, show: e.target.value as Config["show"] })}>
          <option value="all">Tasks and chores</option>
          <option value="tasks">One-off tasks only</option>
          <option value="chores">Repeating chores only</option>
        </select>
      </label>
      <label className="toggle">
        <input type="checkbox" checked={config.completed} onChange={(e) => onChange({ ...config, completed: e.target.checked })} />
        Show completed instead
      </label>
    </div>
  );
}

export const tasksAndChores = defineWidget<Config>({
  id: "agenda.tasks-and-chores",
  version: 1,
  title: "Tasks & chores",
  description: "To-dos and repeating chores, with who they're for",
  configSchema: Config,
  defaultConfig: { show: "all", personId: null, completed: false },
  layout: { w: 5, h: 9, minW: 3, minH: 4 },
  Component: TasksWidget,
  Settings: TasksSettings,
});
