import type { Person, Repeat, Task } from "@agenda/contracts";
import { useState } from "react";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { formatDay, formatTime, localDateOf, today, weekdayCode } from "../dates";

type RepeatChoice = "none" | "weekly" | "monthly" | "after" | "custom";

function buildRepeat(choice: RepeatChoice, due: string, days: number, custom: string): Repeat {
  switch (choice) {
    case "weekly":
      return { mode: "schedule", rrule: `FREQ=WEEKLY;BYDAY=${weekdayCode(due)}` };
    case "monthly":
      return { mode: "schedule", rrule: "FREQ=MONTHLY" };
    case "after":
      return { mode: "after_completion", interval_days: days };
    case "custom":
      return { mode: "schedule", rrule: custom };
    default:
      return { mode: "none" };
  }
}

function describeRepeat(repeat: Repeat): string | null {
  if (repeat.mode === "after_completion") return `every ${repeat.interval_days} days after done`;
  if (repeat.mode === "schedule") return repeat.rrule ?? null;
  return null;
}

export function TasksTab({ spaceId }: { spaceId: string }) {
  const [showDone, setShowDone] = useState(false);
  const people = useLoad(
    async () =>
      (await call((await api()).GET("/api/v1/spaces/{space_id}/people", { params: { path: { space_id: spaceId } } })))
        .data,
    [spaceId],
  );
  const tasks = useLoad(
    async () =>
      (
        await call(
          (await api()).GET("/api/v1/spaces/{space_id}/tasks", {
            params: { path: { space_id: spaceId }, query: { status: showDone ? "done" : "open", limit: 200 } },
          }),
        )
      ).data,
    [spaceId, showDone],
  );
  const { run, error } = useAction(tasks.reload);
  const names = new Map((people.data ?? []).map((p) => [p.id, p.display_name]));

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
    <section>
      <NewTask spaceId={spaceId} people={people.data ?? []} onCreated={tasks.reload} />
      <label className="toggle">
        <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show completed
      </label>
      {(error || tasks.error) && <p className="problem">{error ?? tasks.error}</p>}
      <ul className="rows">
        {(tasks.data ?? []).map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            assignee={task.assignee_person_id ? names.get(task.assignee_person_id) : undefined}
            onToggle={() => patch(task, { status: task.status === "done" ? "open" : "done" })}
            onRename={(title) => patch(task, { title })}
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
      {tasks.data?.length === 0 && <p className="muted">{showDone ? "Nothing completed yet." : "All clear."}</p>}
    </section>
  );
}

function TaskRow(props: {
  task: Task;
  assignee?: string;
  onToggle: () => void;
  onRename: (title: string) => void;
  onArchive: () => void;
}) {
  const { task } = props;
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const dueDay = task.due_date ?? (task.due_at ? localDateOf(task.due_at) : null);
  const overdue = task.status === "open" && dueDay !== null && dueDay < today();
  const repeat = describeRepeat(task.repeat);

  return (
    <li className="row">
      <input
        type="checkbox"
        aria-label={`Complete ${task.title}`}
        checked={task.status === "done"}
        onChange={props.onToggle}
      />
      <div className="grow">
        {editing ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setEditing(false);
              if (title.trim() && title !== task.title) props.onRename(title);
            }}
          >
            <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus onBlur={() => setEditing(false)} />
          </form>
        ) : (
          <span className={task.status === "done" ? "done" : undefined} onDoubleClick={() => setEditing(true)}>
            {task.title}
          </span>
        )}
        <div className="meta">
          {dueDay && (
            <span className={overdue ? "overdue" : undefined}>
              {formatDay(dueDay)}
              {task.due_at && ` ${formatTime(task.due_at)}`}
            </span>
          )}
          {props.assignee && <span className="chip">{props.assignee}</span>}
          {repeat && <span title="Repeats">↻ {repeat}</span>}
        </div>
      </div>
      <button className="quiet" onClick={() => setEditing(true)}>
        Rename
      </button>
      <button className="quiet" onClick={props.onArchive}>
        Archive
      </button>
    </li>
  );
}

function NewTask(props: { spaceId: string; people: Person[]; onCreated: () => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [assignee, setAssignee] = useState("");
  const [repeat, setRepeat] = useState<RepeatChoice>("none");
  const [days, setDays] = useState(7);
  const [custom, setCustom] = useState("FREQ=WEEKLY;BYDAY=MO");
  const { run, error } = useAction(props.onCreated);
  const needsDue = repeat === "weekly" || repeat === "monthly" || repeat === "custom";

  return (
    <form
      className="card form-grid"
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          await call(
            (await api()).POST("/api/v1/spaces/{space_id}/tasks", {
              params: { path: { space_id: props.spaceId } },
              body: {
                title,
                due_date: due || null,
                assignee_person_id: assignee || null,
                repeat: buildRepeat(repeat, due, days, custom),
              },
            }),
          );
          setTitle("");
        });
      }}
    >
      <input className="wide" placeholder="New task or chore" value={title} onChange={(e) => setTitle(e.target.value)} />
      <label>
        Due
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
      </label>
      <label>
        For
        <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
          <option value="">Anyone</option>
          {props.people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.display_name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Repeats
        <select value={repeat} onChange={(e) => setRepeat(e.target.value as RepeatChoice)}>
          <option value="none">Doesn't repeat</option>
          <option value="weekly">Weekly on the due day</option>
          <option value="monthly">Monthly on the due date</option>
          <option value="after">Some days after it's done</option>
          <option value="custom">Custom rule…</option>
        </select>
      </label>
      {repeat === "after" && (
        <label>
          Days
          <input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(Number(e.target.value))} />
        </label>
      )}
      {repeat === "custom" && (
        <label className="wide">
          RRULE
          <input value={custom} onChange={(e) => setCustom(e.target.value)} />
        </label>
      )}
      <button type="submit" disabled={!title.trim() || (needsDue && !due)}>
        Add
      </button>
      {needsDue && !due && <p className="muted wide">Scheduled chores need a due date.</p>}
      {error && <p className="problem wide">{error}</p>}
    </form>
  );
}
