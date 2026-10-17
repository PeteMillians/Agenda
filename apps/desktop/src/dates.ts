// Calendar helpers. The UI works in the browser's local time zone, which is the owner's.

const WEEKDAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

/** YYYY-MM-DD for a local date. */
export function isoDate(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function today(): string {
  return isoDate(new Date());
}

export function addDays(day: string, days: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return isoDate(new Date(y, m - 1, d + days));
}

/** RRULE weekday code (MO, TU, ...) of a YYYY-MM-DD date. */
export function weekdayCode(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  return WEEKDAYS[new Date(y, m - 1, d).getDay()];
}

/** Local date + HH:MM -> UTC ISO string. */
export function localToIso(day: string, time: string): string {
  return new Date(`${day}T${time}`).toISOString();
}

export function localDateOf(iso: string): string {
  return isoDate(new Date(iso));
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function formatDay(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
