/** Fetch every page of a cursor-paged list, following next_cursor until it is null. */
export async function fetchAllPages<T>(
  page: (cursor: string | undefined) => Promise<{ data: T[]; next_cursor: string | null }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const result = await page(cursor);
    items.push(...result.data);
    cursor = result.next_cursor ?? undefined;
  } while (cursor !== undefined);
  return items;
}
