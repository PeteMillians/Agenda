import { describe, expect, it } from "vitest";
import { z } from "zod";
import { defineWidget, resolve } from "./registry";

const counter = defineWidget({
  id: "test.counter",
  version: 2,
  title: "Counter",
  description: "",
  // v1 stored { n: number }; v2 renamed it to { count: number }
  configSchema: z.object({ count: z.number().int().min(0) }),
  defaultConfig: { count: 0 },
  migrateConfig: (old, fromVersion) => (fromVersion === 1 ? { count: (old as { n: number }).n } : old),
  layout: { w: 4, h: 3, minW: 2, minH: 2 },
  Component: () => null,
});
const registry = [counter];
const placement = (widget: string, version: number, config: Record<string, unknown>) => ({
  key: "k",
  widget,
  version,
  config,
});

describe("resolve", () => {
  it("passes through a valid current config", () => {
    expect(resolve(placement("test.counter", 2, { count: 5 }), registry)).toMatchObject({
      status: "ok",
      config: { count: 5 },
      upgraded: false,
    });
  });

  it("reports an unknown widget instead of failing", () => {
    expect(resolve(placement("gone.widget", 1, {}), registry)).toEqual({ status: "unknown", widgetId: "gone.widget" });
  });

  it("migrates an old config", () => {
    expect(resolve(placement("test.counter", 1, { n: 7 }), registry)).toMatchObject({
      status: "ok",
      config: { count: 7 },
      upgraded: true,
    });
  });

  it("falls back to defaults when the config is invalid", () => {
    expect(resolve(placement("test.counter", 2, { count: "lots" }), registry)).toMatchObject({
      config: { count: 0 },
      upgraded: true,
    });
  });

  it("falls back to defaults when the migration throws", () => {
    expect(resolve(placement("test.counter", 1, {}), registry)).toMatchObject({ config: { count: 0 } });
  });
});
