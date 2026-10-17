import { useState } from "react";
import { api } from "../api/backend";
import { call, describe } from "../api/call";

export function Setup({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string>();
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      await call((await api()).POST("/api/v1/setup", { body: { display_name: name, timezone } }));
      onDone();
    } catch (e) {
      setProblem(describe(e));
    }
  }

  return (
    <main className="page setup">
      <h1>Welcome to Agenda</h1>
      <p>Everything stays on this computer. We'll create Personal, Work, and Family spaces to start.</p>
      <form className="stack" onSubmit={submit}>
        <label>
          Your name
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>
        <p className="muted">Time zone: {timezone}</p>
        <button type="submit" disabled={!name.trim()}>
          Get started
        </button>
      </form>
      {problem && <p className="problem">{problem}</p>}
    </main>
  );
}
