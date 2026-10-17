import type { ListItem } from "@agenda/contracts";
import { useState } from "react";
import { z } from "zod";
import { api } from "../api/backend";
import { call, ifMatch, useAction, useLoad } from "../api/call";
import { defineWidget, type WidgetProps } from "../dashboard/registry";
import { SpaceDot, SpacePicker } from "../state/spaces";

const Config = z.object({ listId: z.string().nullable() });
type Config = z.infer<typeof Config>;

function ListsWidget({ config, spaceIds, setConfig }: WidgetProps<Config>) {
  const lists = useLoad(async () => {
    const client = await api();
    const all = await Promise.all(
      spaceIds.map((space_id) => call(client.GET("/api/v1/spaces/{space_id}/lists", { params: { path: { space_id } } }))),
    );
    return all.flatMap((r) => r.data);
  }, [spaceIds.join()]);
  const [creating, setCreating] = useState(false);
  const current = (lists.data ?? []).find((l) => l.id === config.listId) ?? lists.data?.[0];

  return (
    <div className="widget-content">
      <div className="list-picker">
        {(lists.data ?? []).map((list) => (
          <button
            key={list.id}
            className={list.id === current?.id ? "pill active" : "pill"}
            aria-pressed={list.id === current?.id}
            onClick={() => setConfig({ listId: list.id })}
          >
            {spaceIds.length > 1 && <SpaceDot spaceId={list.space_id} />}
            {list.name}
            <span className="muted"> {list.item_count - list.checked_count}</span>
          </button>
        ))}
        <button className="quiet" onClick={() => setCreating(true)}>
          + List
        </button>
      </div>
      {creating && (
        <NewList
          spaceIds={spaceIds}
          onCreated={(listId) => {
            setCreating(false);
            setConfig({ listId });
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {lists.error && <p className="problem">{lists.error}</p>}
      {current ? (
        <Items key={current.id} listId={current.id} />
      ) : (
        lists.data && !creating && <p className="muted">No lists yet.</p>
      )}
    </div>
  );
}

function NewList(props: { spaceIds: string[]; onCreated: (id: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [space, setSpace] = useState(props.spaceIds[0]);
  const { run, error } = useAction(async () => {});
  return (
    <form
      className="form-grid compose"
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          const created = await call(
            (await api()).POST("/api/v1/spaces/{space_id}/lists", { params: { path: { space_id: space } }, body: { name } }),
          );
          props.onCreated(created.data.id);
        });
      }}
    >
      <input className="grow" placeholder="List name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      <SpacePicker spaceIds={props.spaceIds} value={space} onChange={setSpace} />
      <button type="submit" disabled={!name.trim()}>
        Create
      </button>
      <button type="button" className="quiet" onClick={props.onCancel}>
        Cancel
      </button>
      {error && <p className="problem wide">{error}</p>}
    </form>
  );
}

function Items({ listId }: { listId: string }) {
  const detail = useLoad(
    async () => (await call((await api()).GET("/api/v1/lists/{list_id}", { params: { path: { list_id: listId } } }))).data,
    [listId],
  );
  const [text, setText] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const { run, error } = useAction(detail.reload);
  const items = detail.data?.items ?? [];
  const list = detail.data;

  const patchItem = (item: ListItem, body: { checked?: boolean; text?: string }) =>
    run(async () =>
      call(
        (await api()).PATCH("/api/v1/list-items/{item_id}", {
          params: { path: { item_id: item.id }, header: ifMatch(item.revision) },
          body,
        }),
      ),
    );

  return (
    <div>
      <div className="list-head">
        {renaming && list ? (
          <form
            className="grow"
            onSubmit={(e) => {
              e.preventDefault();
              setRenaming(false);
              void run(async () =>
                call(
                  (await api()).PATCH("/api/v1/lists/{list_id}", {
                    params: { path: { list_id: listId }, header: ifMatch(list.revision) },
                    body: { name },
                  }),
                ),
              );
            }}
          >
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus onBlur={() => setRenaming(false)} />
          </form>
        ) : (
          <strong className="grow">{list?.name}</strong>
        )}
        {list && !renaming && (
          <>
            <button className="quiet" onClick={() => (setName(list.name), setRenaming(true))}>
              Rename
            </button>
            <button
              className="quiet"
              onClick={() =>
                run(async () =>
                  call(
                    (await api()).DELETE("/api/v1/lists/{list_id}", {
                      params: { path: { list_id: listId }, header: ifMatch(list.revision) },
                    }),
                  ),
                )
              }
            >
              Archive list
            </button>
          </>
        )}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await call((await api()).POST("/api/v1/lists/{list_id}/items", { params: { path: { list_id: listId } }, body: { text } }));
            setText("");
          });
        }}
      >
        <input placeholder="Add item" value={text} onChange={(e) => setText(e.target.value)} />
      </form>
      <ul className="rows">
        {items.map((item) => (
          <li key={item.id} className="row">
            <input
              type="checkbox"
              aria-label={`Check ${item.text}`}
              checked={item.checked}
              onChange={() => patchItem(item, { checked: !item.checked })}
            />
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

export const lists = defineWidget<Config>({
  id: "agenda.lists",
  version: 1,
  title: "Lists",
  description: "Shopping and packing lists you can tick off",
  configSchema: Config,
  defaultConfig: { listId: null },
  layout: { w: 4, h: 8, minW: 3, minH: 4 },
  Component: ListsWidget,
});
