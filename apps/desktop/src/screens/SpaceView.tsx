import type { Space } from "@agenda/contracts";
import { useState } from "react";
import { EventsTab } from "./EventsTab";
import { ListsTab } from "./ListsTab";
import { PeopleTab } from "./PeopleTab";
import { TasksTab } from "./TasksTab";

const TABS = ["Tasks & chores", "Calendar", "Lists", "People"] as const;
type Tab = (typeof TABS)[number];

export function SpaceView({ space }: { space: Space }) {
  const [tab, setTab] = useState<Tab>("Tasks & chores");
  return (
    <>
      <header className="space-header">
        <h2>
          <span className="dot" style={{ background: space.color }} />
          {space.name}
        </h2>
        <div className="tabs" role="tablist">
          {TABS.map((name) => (
            <button key={name} role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
              {name}
            </button>
          ))}
        </div>
      </header>
      {tab === "Tasks & chores" && <TasksTab spaceId={space.id} />}
      {tab === "Calendar" && <EventsTab spaceId={space.id} />}
      {tab === "Lists" && <ListsTab spaceId={space.id} />}
      {tab === "People" && <PeopleTab spaceId={space.id} />}
    </>
  );
}
