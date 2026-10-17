// The built-in widget registry (spec §12). Never reuse an id for a different widget.
import type { WidgetPlacement } from "@agenda/contracts";
import type { AnyWidget } from "../dashboard/registry";
import { upcomingEvents } from "./EventsWidget";
import { lists } from "./ListsWidget";
import { people } from "./PeopleWidget";
import { tasksAndChores } from "./TasksWidget";

export const widgetRegistry: readonly AnyWidget[] = [upcomingEvents, tasksAndChores, lists, people];

export function newPlacement(widget: AnyWidget, at: { x: number; y: number }): WidgetPlacement {
  return {
    key: `${widget.id.split(".").pop()}-${crypto.randomUUID().slice(0, 8)}`,
    widget: widget.id,
    version: widget.version,
    config: widget.defaultConfig as Record<string, unknown>,
    x: at.x,
    y: at.y,
    w: widget.layout.w,
    h: widget.layout.h,
  };
}

/** What a new dashboard starts with: calendar and tasks side by side, a list below. */
export function defaultLayout(): WidgetPlacement[] {
  return [
    { ...newPlacement(upcomingEvents, { x: 0, y: 0 }), w: 7, h: 10 },
    { ...newPlacement(tasksAndChores, { x: 7, y: 0 }), w: 5, h: 10 },
    { ...newPlacement(lists, { x: 0, y: 10 }), w: 5, h: 8 },
  ];
}
