import type { Space } from "@agenda/contracts";
import { createContext, type ReactNode, useContext } from "react";
import { api } from "../api/backend";
import { call, useLoad } from "../api/call";

interface SpacesState {
  spaces: Space[];
  byId: Map<string, Space>;
  reload: () => Promise<void>;
}

const SpacesContext = createContext<SpacesState>({ spaces: [], byId: new Map(), reload: async () => {} });

/** Active spaces, shared by every widget so each can label items with their space. */
export function SpacesProvider({ children }: { children: ReactNode }) {
  const loaded = useLoad(async () => (await call((await api()).GET("/api/v1/spaces"))).data, []);
  const spaces = loaded.data ?? [];
  return (
    <SpacesContext.Provider value={{ spaces, byId: new Map(spaces.map((s) => [s.id, s])), reload: loaded.reload }}>
      {children}
    </SpacesContext.Provider>
  );
}

export function useSpaces(): SpacesState {
  return useContext(SpacesContext);
}

export function SpaceDot({ spaceId }: { spaceId: string }) {
  const space = useSpaces().byId.get(spaceId);
  if (!space) return null;
  return <span className="dot" style={{ background: space.color }} title={space.name} aria-label={space.name} />;
}

/** Space chooser for create forms; hidden when the widget shows a single space. */
export function SpacePicker(props: { spaceIds: string[]; value: string; onChange: (id: string) => void }) {
  const { byId } = useSpaces();
  if (props.spaceIds.length < 2) return null;
  return (
    <label>
      Space
      <select value={props.value} onChange={(e) => props.onChange(e.target.value)}>
        {props.spaceIds.map((id) => (
          <option key={id} value={id}>
            {byId.get(id)?.name ?? "…"}
          </option>
        ))}
      </select>
    </label>
  );
}
