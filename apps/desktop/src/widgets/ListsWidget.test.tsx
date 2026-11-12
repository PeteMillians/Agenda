import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { lists } from "./ListsWidget";

const list = { id: "list-1", space_id: "space-1", name: "Groceries", item_count: 0, checked_count: 0, revision: 7 };

const GET = vi.fn(async (path: string) => ({
  data: path === "/api/v1/spaces/{space_id}/lists" ? { data: [list], next_cursor: null } : { data: { ...list, items: [] } },
  response: { ok: true },
}));
const DELETE = vi.fn(async () => ({ data: undefined, response: { ok: true } }));

vi.mock("../api/backend", () => ({ api: async () => ({ GET, DELETE }) }));

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

const noop = () => {};

function renderWidget() {
  render(<lists.Component config={{ listId: null }} spaceIds={["space-1"]} setConfig={noop} />);
}

describe("Lists widget archive", () => {
  it("asks before archiving", async () => {
    renderWidget();
    fireEvent.click(await screen.findByRole("button", { name: "Archive list" }));
    expect(DELETE).not.toHaveBeenCalled();
    expect(screen.getByText("This list and its items will be removed from Agenda.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Archive" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("archives once confirmed", async () => {
    renderWidget();
    fireEvent.click(await screen.findByRole("button", { name: "Archive list" }));
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    await vi.waitFor(() => expect(DELETE).toHaveBeenCalledTimes(1));
    expect(DELETE).toHaveBeenCalledWith("/api/v1/lists/{list_id}", {
      params: { path: { list_id: "list-1" }, header: { "if-match": "7" } },
    });
  });

  it("cancels without a call", async () => {
    renderWidget();
    fireEvent.click(await screen.findByRole("button", { name: "Archive list" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(DELETE).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Rename" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Archive list" })).toBeTruthy();
  });
});
