// Typed widget registry (spec §12). Widgets are built-in React components; the backend stores
// each placement's config, and this file decides what that config means.
import type { FC } from "react";
import type { z } from "zod";

export interface WidgetProps<Config> {
  config: Config;
  /** Spaces the dashboard shows. Widgets never get more authority than this. */
  spaceIds: string[];
  /** Persist a config change made from inside the widget (e.g. the list it is showing). */
  setConfig: (config: Config) => void;
}

export interface WidgetDefinition<Config> {
  id: string; // stable forever, e.g. "agenda.upcoming-events"; never reused for another widget
  version: number; // bump when the config shape changes, and add a migrateConfig step
  title: string;
  description: string;
  configSchema: z.ZodType<Config>;
  defaultConfig: Config;
  migrateConfig?: (old: unknown, fromVersion: number) => unknown;
  layout: { w: number; h: number; minW: number; minH: number };
  Component: FC<WidgetProps<Config>>;
  /** Optional settings form shown while customizing. */
  Settings?: FC<{ config: Config; onChange: (config: Config) => void; spaceIds: string[] }>;
}

/** Keeps the Config type parameter checked inside each definition. */
export function defineWidget<Config>(definition: WidgetDefinition<Config>): WidgetDefinition<Config> {
  return definition;
}

// The registry holds definitions with different config types.
export type AnyWidget = WidgetDefinition<any>;

/** What a stored placement points at. */
export interface StoredPlacement {
  key: string;
  widget: string;
  version: number;
  config?: Record<string, unknown>; // missing means "use the defaults"
}

export type Resolved =
  | { status: "ok"; definition: AnyWidget; config: unknown; upgraded: boolean }
  | { status: "unknown"; widgetId: string };

/** Find the widget, migrate an old config, validate it, and fall back to defaults if invalid. */
export function resolve(placement: StoredPlacement, registry: readonly AnyWidget[]): Resolved {
  const definition = registry.find((w) => w.id === placement.widget);
  if (!definition) return { status: "unknown", widgetId: placement.widget };

  let raw: unknown = placement.config;
  if (placement.version < definition.version && definition.migrateConfig) {
    try {
      raw = definition.migrateConfig(raw, placement.version);
    } catch {
      raw = undefined; // a failed migration falls through to the defaults below
    }
  }
  const parsed = definition.configSchema.safeParse(raw);
  return {
    status: "ok",
    definition,
    config: parsed.success ? parsed.data : definition.defaultConfig,
    upgraded: placement.version !== definition.version || !parsed.success,
  };
}
