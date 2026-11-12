import { describe, expect, it, vi } from "vitest";
import { fetchAllPages } from "./pages";

describe("fetchAllPages", () => {
  it("returns a single page's items", async () => {
    const page = vi.fn(async () => ({ data: [1, 2], next_cursor: null }));
    expect(await fetchAllPages(page)).toEqual([1, 2]);
    expect(page).toHaveBeenCalledTimes(1);
    expect(page).toHaveBeenCalledWith(undefined);
  });

  it("follows each cursor and keeps page order", async () => {
    const pages: Record<string, { data: number[]; next_cursor: string | null }> = {
      start: { data: [1, 2], next_cursor: "b" },
      b: { data: [3, 4], next_cursor: "c" },
      c: { data: [5], next_cursor: null },
    };
    const page = vi.fn(async (cursor: string | undefined) => pages[cursor ?? "start"]);
    expect(await fetchAllPages(page)).toEqual([1, 2, 3, 4, 5]);
    expect(page.mock.calls).toEqual([[undefined], ["b"], ["c"]]);
  });

  it("rejects with the error from a later page", async () => {
    const failure = new Error("page 2 failed");
    const page = vi
      .fn<(cursor: string | undefined) => Promise<{ data: number[]; next_cursor: string | null }>>()
      .mockResolvedValueOnce({ data: [1], next_cursor: "b" })
      .mockRejectedValueOnce(failure);
    await expect(fetchAllPages(page)).rejects.toBe(failure);
    expect(page).toHaveBeenCalledTimes(2);
  });
});
