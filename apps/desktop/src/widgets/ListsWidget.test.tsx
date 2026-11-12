import type { ListItem } from "@agenda/contracts";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ GET: vi.fn(), PATCH: vi.fn() }));
vi.mock("../api/backend", () => ({ api: async () => client }));

import { lists } from "./ListsWidget";

afterEach(cleanup);

const ok = (data: unknown) => Promise.resolve({ data, response: { ok: true } });

function item(id: string, text: string, position: number): ListItem {
  return {
    id,
    list_id: "list-1",
    text,
    checked: false,
    position,
    revision: position + 3,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const list = { id: "list-1", space_id: "space-1", name: "Groceries", revision: 1, item_count: 2, checked_count: 0 };
let items: ListItem[];

beforeEach(() => {
  items = [item("item-milk", "Milk", 0), item("item-eggs", "Eggs", 1)];
  client.GET.mockReset().mockImplementation((path: string) =>
    path === "/api/v1/spaces/{space_id}/lists" ? ok({ data: [list] }) : ok({ data: { ...list, archived_at: null, items } }),
  );
  client.PATCH.mockReset().mockImplementation(() => ok({}));
});

async function renderWidget() {
  render(<lists.Component config={{ listId: "list-1" }} spaceIds={["space-1"]} setConfig={() => {}} />);
  await screen.findByText("Milk");
}

async function startEditing() {
  await renderWidget();
  fireEvent.click(screen.getByRole("button", { name: "Edit Milk" }));
  return screen.getByRole("textbox", { name: "New text for Milk" }) as HTMLInputElement;
}

describe("ListsWidget item editing", () => {
  it("shows an Edit button on each item", async () => {
    await renderWidget();
    expect(screen.getByRole("button", { name: "Edit Milk" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit Eggs" })).toBeTruthy();
  });

  it("opens a focused, prefilled input and keeps the checkbox and remove button", async () => {
    const input = await startEditing();
    expect(input.value).toBe("Milk");
    expect(document.activeElement).toBe(input);
    expect(screen.queryByRole("button", { name: "Edit Milk" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "Check Milk" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove Milk" })).toBeTruthy();
  });

  it("sends one PATCH with only the new text and the revision", async () => {
    const input = await startEditing();
    fireEvent.change(input, { target: { value: "Oat milk" } });
    fireEvent.submit(input.form!);
    await waitFor(() => expect(client.PATCH).toHaveBeenCalledTimes(1));
    const [path, options] = client.PATCH.mock.calls[0];
    expect(path).toBe("/api/v1/list-items/{item_id}");
    expect(options.params.path.item_id).toBe("item-milk");
    expect(options.params.header["if-match"]).toBe(String(items[0].revision));
    expect(options.body).toEqual({ text: "Oat milk" });
  });

  it("sends nothing when the text is unchanged", async () => {
    const input = await startEditing();
    fireEvent.submit(input.form!);
    expect(client.PATCH).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "New text for Milk" })).toBeNull();
    expect(screen.getByText("Milk")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit Milk" })).toBeTruthy();
  });

  it("sends nothing when the text is only whitespace", async () => {
    const input = await startEditing();
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.submit(input.form!);
    expect(client.PATCH).not.toHaveBeenCalled();
    expect(screen.getByText("Milk")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit Milk" })).toBeTruthy();
  });

  it("cancels on Escape", async () => {
    const input = await startEditing();
    fireEvent.change(input, { target: { value: "Changed" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(client.PATCH).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "New text for Milk" })).toBeNull();
    expect(screen.getByText("Milk")).toBeTruthy();
  });

  it("cancels on blur", async () => {
    const input = await startEditing();
    fireEvent.change(input, { target: { value: "Changed" } });
    fireEvent.blur(input);
    expect(client.PATCH).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "New text for Milk" })).toBeNull();
    expect(screen.getByText("Milk")).toBeTruthy();
  });

  it("keeps the item in place after saving", async () => {
    const input = await startEditing();
    client.PATCH.mockImplementation(() => {
      items = [{ ...items[0], text: "Oat milk", revision: items[0].revision + 1 }, items[1]];
      return ok({});
    });
    fireEvent.change(input, { target: { value: "Oat milk" } });
    fireEvent.submit(input.form!);
    await screen.findByText("Oat milk");
    const rows = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Oat milk"),
      expect.stringContaining("Eggs"),
    ]);
  });

  it("shows a failed save in the error line", async () => {
    client.PATCH.mockImplementation(() =>
      Promise.resolve({
        response: { ok: false, status: 409 },
        error: { error: { code: "REVISION_CONFLICT", message: "x" } },
      }),
    );
    const input = await startEditing();
    fireEvent.change(input, { target: { value: "Oat milk" } });
    fireEvent.submit(input.form!);
    expect(await screen.findByText("That changed somewhere else, so it was reloaded. Please try again.")).toBeTruthy();
  });

  it("still ticks items with the checkbox", async () => {
    await renderWidget();
    fireEvent.click(screen.getByRole("checkbox", { name: "Check Milk" }));
    await waitFor(() => expect(client.PATCH).toHaveBeenCalledTimes(1));
    expect(client.PATCH.mock.calls[0][1].body).toEqual({ checked: true });
  });
});
