import type { ListItem } from "@agenda/contracts";
import { useState } from "react";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";

export function ListsTab({ spaceId }: { spaceId: string }) {
  const lists = useLoad(
    async () =>
      (await call((await api()).GET("/api/v1/spaces/{space_id}/lists", { params: { path: { space_id: spaceId } } })))
        .data,
    [spaceId],
  );
  const [selected, setSelected] = useState<string>();
  const [name, setName] = useState("");
  const { run, error } = useAction(lists.reload);
  const current = (lists.data ?? []).find((l) => l.id === selected) ?? lists.data?.[0];

  return (
    <section className="split">
      <div>
        <ul className="rows">
          {(lists.data ?? []).map((list) => (
            <li key={list.id}>
              <button className={list.id === current?.id ? "space active" : "space"} onClick={() => setSelected(list.id)}>
                {list.name}
                <span className="muted">
                  {" "}
                  {list.item_count - list.checked_count}/{list.item_count}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const created = await call(
                (await api()).POST("/api/v1/spaces/{space_id}/lists", {
                  params: { path: { space_id: spaceId } },
                  body: { name },
                }),
              );
              setSelected(created.data.id);
              setName("");
            });
          }}
        >
          <input placeholder="New list" value={name} onChange={(e) => setName(e.target.value)} />
        </form>
        {(error || lists.error) && <p className="problem">{error ?? lists.error}</p>}
      </div>
      {current ? <ListItems key={current.id} listId={current.id} onChange={lists.reload} /> : <p className="muted">No lists yet.</p>}
    </section>
  );
}

function ListItems({ listId, onChange }: { listId: string; onChange: () => Promise<void> }) {
  const detail = useLoad(
    async () => (await call((await api()).GET("/api/v1/lists/{list_id}", { params: { path: { list_id: listId } } }))).data,
    [listId],
  );
  const [text, setText] = useState("");
  const { run, error } = useAction(async () => {
    await detail.reload();
    await onChange();
  });

  const patch = (item: ListItem, body: { checked?: boolean }) =>
    run(async () =>
      call(
        (await api()).PATCH("/api/v1/list-items/{item_id}", {
          params: { path: { item_id: item.id }, header: ifMatch(item.revision) },
          body,
        }),
      ),
    );
  const items = detail.data?.items ?? [];

  return (
    <div className="card">
      <h3>{detail.data?.name}</h3>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await call((await api()).POST("/api/v1/lists/{list_id}/items", { params: { path: { list_id: listId } }, body: { text } }));
            setText("");
          });
        }}
      >
        <input placeholder="Add item" value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      </form>
      <ul className="rows">
        {items.map((item) => (
          <li key={item.id} className="row">
            <input type="checkbox" checked={item.checked} onChange={() => patch(item, { checked: !item.checked })} />
            <span className={item.checked ? "grow done" : "grow"}>{item.text}</span>
            <button
              className="quiet"
              aria-label={`Remove ${item.text}`}
              onClick={() =>
                run(async () =>
                  call(
                    (await api()).DELETE("/api/v1/list-items/{item_id}", {
                      params: { path: { item_id: item.id }, header: ifMatch(item.revision) },
                    }),
                  ),
                )
              }
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {items.some((i) => i.checked) && (
        <button
          className="quiet"
          onClick={() =>
            run(async () => call((await api()).POST("/api/v1/lists/{list_id}/clear-checked", { params: { path: { list_id: listId } } })))
          }
        >
          Clear checked
        </button>
      )}
      {(error || detail.error) && <p className="problem">{error ?? detail.error}</p>}
    </div>
  );
}
