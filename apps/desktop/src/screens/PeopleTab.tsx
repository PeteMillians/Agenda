import type { Person } from "@agenda/contracts";
import { useState } from "react";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";

export function PeopleTab({ spaceId }: { spaceId: string }) {
  const people = useLoad(
    async () =>
      (await call((await api()).GET("/api/v1/spaces/{space_id}/people", { params: { path: { space_id: spaceId } } })))
        .data,
    [spaceId],
  );
  const [name, setName] = useState("");
  const { run, error } = useAction(people.reload);

  return (
    <section>
      <p className="muted">People can be assigned tasks and chores. They don't need an Agenda login.</p>
      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await call(
              (await api()).POST("/api/v1/spaces/{space_id}/people", {
                params: { path: { space_id: spaceId } },
                body: { display_name: name },
              }),
            );
            setName("");
          });
        }}
      >
        <input placeholder="Add a person" value={name} onChange={(e) => setName(e.target.value)} />
      </form>
      {(error || people.error) && <p className="problem">{error ?? people.error}</p>}
      <ul className="rows">
        {(people.data ?? []).map((person) => (
          <PersonRow
            key={person.id}
            person={person}
            onRename={(display_name) =>
              run(async () =>
                call(
                  (await api()).PATCH("/api/v1/people/{person_id}", {
                    params: { path: { person_id: person.id }, header: ifMatch(person.revision) },
                    body: { display_name },
                  }),
                ),
              )
            }
            onRemove={() =>
              run(async () =>
                call(
                  (await api()).DELETE("/api/v1/people/{person_id}", {
                    params: { path: { person_id: person.id }, header: ifMatch(person.revision) },
                  }),
                ),
              )
            }
          />
        ))}
      </ul>
    </section>
  );
}

function PersonRow(props: { person: Person; onRename: (name: string) => void; onRemove: () => void }) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [name, setName] = useState(props.person.display_name);
  return (
    <li className="row">
      <span className="dot" style={{ background: props.person.color }} />
      {editing ? (
        <form
          className="grow"
          onSubmit={(e) => {
            e.preventDefault();
            setEditing(false);
            if (name.trim()) props.onRename(name);
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus onBlur={() => setEditing(false)} />
        </form>
      ) : (
        <span className="grow">{props.person.display_name}</span>
      )}
      {confirming ? (
        <span className="scope">
          Their open tasks become unassigned.
          <button onClick={props.onRemove}>Remove</button>
          <button className="quiet" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </span>
      ) : (
        <>
          <button className="quiet" onClick={() => setEditing(true)}>
            Rename
          </button>
          <button className="quiet" onClick={() => setConfirming(true)}>
            Remove
          </button>
        </>
      )}
    </li>
  );
}
