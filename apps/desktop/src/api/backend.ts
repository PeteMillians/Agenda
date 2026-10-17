// The WebView's two pipes (docs/Agenda_Architecture.html §01):
//   Tauri IPC  -> where the backend is and which secret to send, plus lifecycle events
//   HTTP       -> everything else, typed from the backend's OpenAPI document
import type { paths } from "@agenda/contracts";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import createClient, { type Client } from "openapi-fetch";

type BackendConfig = { base_url: string; token: string };

export type BackendStatus =
  | { state: "starting" }
  | { state: "ready" }
  | { state: "restarting"; attempt: number; retryInMs: number }
  | { state: "failed" };

let client: Client<paths> | null = null;

/** Plain-browser development (UI tests against a hand-started backend): there is no Tauri, so
 * the address and secret come from VITE_AGENDA_* env vars. Compiled out of production builds. */
const browserDev: BackendConfig | null =
  import.meta.env.DEV && !("__TAURI_INTERNALS__" in window) && import.meta.env.VITE_AGENDA_BASE_URL
    ? { base_url: import.meta.env.VITE_AGENDA_BASE_URL, token: import.meta.env.VITE_AGENDA_TOKEN ?? "" }
    : null;

/** Typed API client. Rebuilt after every backend restart: the port and secret both change. */
export async function api(): Promise<Client<paths>> {
  if (client) return client;
  const config = browserDev ?? (await invoke<BackendConfig>("get_backend_config"));
  const fresh = createClient<paths>({ baseUrl: config.base_url });
  fresh.use({
    onRequest({ request }) {
      request.headers.set("Authorization", `Bearer ${config.token}`);
      return request;
    },
  });
  client = fresh;
  return fresh;
}

/** Report backend lifecycle changes. Returns an unsubscribe function. */
export function watchBackend(onStatus: (status: BackendStatus) => void): () => void {
  if (browserDev) {
    onStatus({ state: "ready" });
    return () => {};
  }
  const subscriptions = [
    listen("backend:ready", () => {
      client = null;
      onStatus({ state: "ready" });
    }),
    listen<{ attempt: number; retry_in_ms: number }>("backend:restarting", (event) => {
      client = null;
      onStatus({ state: "restarting", attempt: event.payload.attempt, retryInMs: event.payload.retry_in_ms });
    }),
    listen("backend:failed", () => {
      client = null;
      onStatus({ state: "failed" });
    }),
  ];
  // Subscribe first, then ask for the current state, so an event between the two is not lost.
  Promise.all(subscriptions).then(() =>
    api().then(
      () => onStatus({ state: "ready" }),
      (reason) => onStatus(reason === "failed" ? { state: "failed" } : { state: "starting" }),
    ),
  );
  return () => subscriptions.forEach((subscription) => subscription.then((unlisten) => unlisten()));
}

/** Open a URL in the user's default browser (OAuth sign-in must not happen inside the app). */
export async function openExternal(url: string): Promise<void> {
  if (browserDev) {
    window.open(url, "_blank", "noopener");
    return;
  }
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
}
