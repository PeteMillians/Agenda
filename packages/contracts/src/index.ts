// Types generated from services/backend's OpenAPI document. Never edit schema.d.ts by hand;
// run `pnpm contracts` from the repo root after changing a Pydantic model or route.
import type { components } from "./schema";

export type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type ErrorEnvelope = Schemas["ErrorEnvelope"];
export type User = Schemas["User"];
export type Space = Schemas["Space"];
export type Person = Schemas["Person"];
export type Task = Schemas["Task"];
export type TaskCreate = Schemas["TaskCreate"];
export type Repeat = Schemas["Repeat"];
export type ListSummary = Schemas["ListSummary"];
export type ListDetail = Schemas["ListDetail"];
export type ListItem = Schemas["ListItem"];
export type Occurrence = Schemas["Occurrence"];
export type EventCreate = Schemas["EventCreate"];
export type EventPatch = Schemas["EventPatch"];
export type Dashboard = Schemas["Dashboard"];
export type WidgetPlacement = Schemas["WidgetPlacement"];
export type ExternalAccount = Schemas["ExternalAccount"];
export type CalendarSource = Schemas["CalendarSource"];
export type OAuthClientStatus = Schemas["OAuthClientStatus"];
export type SyncConflict = Schemas["SyncConflict"];
