// Grid geometry and keyboard moves. Pure functions so they are easy to test.
import { cloneLayout, verticalCompactor } from "react-grid-layout/core";

export const COLUMNS = 12;
export const ROW_HEIGHT = 40;

export interface Box {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
  minW?: number;
  minH?: number;
}

export type Direction = "left" | "right" | "up" | "down";

const overlapsColumns = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w;

/** Keyboard move: left/right shift a column; up/down swap places with the neighbour above or
 * below (moving by one row would simply be compacted back). */
export function move(layout: readonly Box[], key: string, direction: Direction): Box[] {
  const next = cloneLayout(layout as Box[]) as Box[];
  const item = next.find((b) => b.i === key);
  if (!item) return next;
  if (direction === "left") item.x = Math.max(0, item.x - 1);
  if (direction === "right") item.x = Math.min(COLUMNS - item.w, item.x + 1);
  if (direction === "down") {
    const below = next
      .filter((b) => b !== item && b.y >= item.y + item.h && overlapsColumns(b, item))
      .sort((a, b) => a.y - b.y)[0];
    if (below) item.y = below.y + below.h; // jump past it; compaction pulls it up into our old spot
  }
  if (direction === "up") {
    const above = next
      .filter((b) => b !== item && b.y + b.h <= item.y && overlapsColumns(b, item))
      .sort((a, b) => b.y - a.y)[0];
    if (above) {
      item.y = above.y;
      above.y = above.y + item.h; // it moves below us
    }
  }
  return verticalCompactor.compact(next as never, COLUMNS) as Box[];
}

/** Keyboard resize: right/left change width, down/up change height, within the widget's minimums. */
export function resize(layout: readonly Box[], key: string, direction: Direction): Box[] {
  const next = cloneLayout(layout as Box[]) as Box[];
  const item = next.find((b) => b.i === key);
  if (!item) return next;
  if (direction === "right") item.w = Math.min(COLUMNS - item.x, item.w + 1);
  if (direction === "left") item.w = Math.max(item.minW ?? 1, item.w - 1);
  if (direction === "down") item.h = Math.min(50, item.h + 1);
  if (direction === "up") item.h = Math.max(item.minH ?? 1, item.h - 1);
  return verticalCompactor.compact(next as never, COLUMNS) as Box[];
}

/** Where a newly added widget goes: the left edge, below everything else. */
export function placeNew(layout: readonly Box[]): { x: number; y: number } {
  return { x: 0, y: layout.reduce((max, b) => Math.max(max, b.y + b.h), 0) };
}
