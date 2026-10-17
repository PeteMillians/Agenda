import type { ErrorEnvelope } from "@agenda/contracts";
import { useCallback, useEffect, useState } from "react";

/** A non-2xx response, carrying the backend's error envelope (spec §08). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly envelope: ErrorEnvelope | undefined,
  ) {
    super(envelope ? envelope.error.message : `Request failed (${status}).`);
  }

  get code(): string {
    return this.envelope?.error.code ?? "UNKNOWN";
  }
}

/** Unwrap an openapi-fetch result: the data, or throw ApiError. */
export async function call<T>(request: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await request;
  if (!response.ok) throw new ApiError(response.status, error as ErrorEnvelope | undefined);
  return data as T;
}

/** If-Match header for optimistic concurrency. */
export function ifMatch(revision: number) {
  return { "if-match": String(revision) };
}

export function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** Load data, with a reload function for after mutations. */
export function useLoad<T>(load: () => Promise<T>, deps: readonly unknown[]) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const reload = useCallback(load, deps);
  const refresh = useCallback(async () => {
    try {
      setData(await reload());
      setError(undefined);
    } catch (e) {
      setError(describe(e));
    }
  }, [reload]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { data, error, reload: refresh };
}

/** Run a mutation, then reload. A revision conflict reloads too, so the next try uses fresh data. */
export function useAction(reload: () => Promise<void>) {
  const [error, setError] = useState<string>();
  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setError(undefined);
      try {
        await action();
      } catch (e) {
        setError(
          e instanceof ApiError && e.code === "REVISION_CONFLICT"
            ? "That changed somewhere else, so it was reloaded. Please try again."
            : describe(e),
        );
      }
      await reload();
    },
    [reload],
  );
  return { run, error, clearError: () => setError(undefined) };
}
