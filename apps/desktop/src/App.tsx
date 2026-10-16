import type { ErrorEnvelope, Note } from "@agenda/contracts";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, type BackendStatus, watchBackend } from "./api/backend";
import "./App.css";

const STATUS_TEXT: Record<BackendStatus["state"], string> = {
  starting: "Starting backend…",
  ready: "Connected",
  restarting: "Backend restarting…",
  failed: "Backend failed to start",
};

function describe(error: ErrorEnvelope | undefined): string {
  return error ? `${error.error.code}: ${error.error.message}` : "Unexpected response from backend.";
}

export default function App() {
  const [status, setStatus] = useState<BackendStatus>({ state: "starting" });
  const [notes, setNotes] = useState<Note[]>([]);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => watchBackend(setStatus), []);

  const refresh = useCallback(async () => {
    try {
      const { data, error } = await (await api()).GET("/api/v1/notes");
      if (data) setNotes(data.data);
      else setProblem(describe(error));
    } catch {
      setProblem("Backend unavailable.");
    }
  }, []);

  useEffect(() => {
    if (status.state === "ready") void refresh();
  }, [status, refresh]);

  async function addNote(event: FormEvent) {
    event.preventDefault();
    setProblem(null);
    try {
      const { data, error } = await (await api()).POST("/api/v1/notes", { body: { body: draft } });
      if (!data) return setProblem(describe(error));
      setNotes((current) => [data.data, ...current]);
      setDraft("");
    } catch {
      setProblem("Backend unavailable.");
    }
  }

  const ready = status.state === "ready";
  return (
    <main className="shell">
      <header>
        <h1>Agenda</h1>
        <span className={`status status-${status.state}`} role="status">
          {STATUS_TEXT[status.state]}
          {status.state === "restarting" && ` (attempt ${status.attempt})`}
        </span>
      </header>

      <form onSubmit={addNote}>
        <input
          aria-label="New note"
          placeholder="Write a note, quit from the tray, relaunch: it should still be here"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={!ready}
        />
        <button type="submit" disabled={!ready || draft.trim() === ""}>
          Add
        </button>
      </form>

      {problem && <p className="problem">{problem}</p>}

      <ul className="notes">
        {notes.map((note) => (
          <li key={note.id}>
            <span>{note.body}</span>
            <time dateTime={note.created_at}>{new Date(note.created_at).toLocaleString()}</time>
          </li>
        ))}
      </ul>
      {ready && notes.length === 0 && <p className="empty">No notes yet.</p>}

      <footer>Phase 0 vertical slice · closing the window hides Agenda to the tray</footer>
    </main>
  );
}
