import { describe, expect, it } from "vitest";
import { type Box, move, placeNew, resize } from "./layout";

const layout: Box[] = [
  { i: "a", x: 0, y: 0, w: 6, h: 3, minW: 3, minH: 2 },
  { i: "b", x: 0, y: 3, w: 6, h: 4 },
  { i: "c", x: 6, y: 0, w: 6, h: 2 },
];
const at = (boxes: Box[], key: string) => boxes.find((b) => b.i === key)!;

describe("keyboard move", () => {
  it("shifts columns and stops at the edges", () => {
    expect(at(move(layout, "a", "right"), "a").x).toBe(1);
    expect(at(move(layout, "a", "left"), "a").x).toBe(0);
    expect(at(move(layout, "c", "right"), "c").x).toBe(6);
  });

  it("swaps with the widget below", () => {
    const next = move(layout, "a", "down");
    expect(at(next, "b").y).toBe(0);
    expect(at(next, "a").y).toBe(4);
  });

  it("swaps with the widget above", () => {
    const next = move(layout, "b", "up");
    expect(at(next, "b").y).toBe(0);
    expect(at(next, "a").y).toBe(4);
  });

  it("does not mutate its input", () => {
    move(layout, "a", "down");
    expect(at(layout, "a").y).toBe(0);
  });
});

describe("keyboard resize", () => {
  it("respects minimums and the grid edge", () => {
    expect(at(resize(layout, "a", "left"), "a").w).toBe(5);
    let shrunk = layout;
    for (let n = 0; n < 10; n++) shrunk = resize(shrunk, "a", "left");
    expect(at(shrunk, "a").w).toBe(3);
    expect(at(resize(layout, "c", "right"), "c").w).toBe(6);
    expect(at(resize(layout, "a", "up"), "a").h).toBe(2);
  });

  it("pushes widgets below out of the way when growing", () => {
    expect(at(resize(layout, "a", "down"), "b").y).toBe(4);
  });
});

it("places new widgets below everything", () => {
  expect(placeNew(layout)).toEqual({ x: 0, y: 7 });
});
