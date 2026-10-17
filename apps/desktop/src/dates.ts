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

const DAY_NAMES: Record<string, string> = { MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun" };
const UNITS: Record<string, [string, string]> = {
  DAILY: ["daily", "days"],
  WEEKLY: ["weekly", "weeks"],
  MONTHLY: ["monthly", "months"],
  YEARLY: ["yearly", "years"],
};

/** "weekly on Tue", "every 2 weeks on Mon, Wed", "monthly until Dec 31". Falls back to the raw rule. */
export function describeRule(rule: string): string {
  const parts = Object.fromEntries(rule.split(";").map((p) => p.split("=") as [string, string]));
  const unit = UNITS[parts.FREQ];
  const known = new Set(["FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT"]);
  if (!unit || Object.keys(parts).some((k) => !known.has(k)) || /\d/.test(parts.BYDAY ?? "")) return rule;
  const interval = Number(parts.INTERVAL ?? 1);
  let text = interval > 1 ? `every ${interval} ${unit[1]}` : unit[0];
  if (parts.BYDAY) text += ` on ${parts.BYDAY.split(",").map((d) => DAY_NAMES[d] ?? d).join(", ")}`;
  if (parts.UNTIL) {
    const u = parts.UNTIL;
    text += ` until ${formatDay(`${u.slice(0, 4)}-${u.slice(4, 6)}-${u.slice(6, 8)}`)}`;
    // a split series ends just before an occurrence (e.g. 08:59:59); say so, or the date reads as included
    const time = u.slice(9, 15);
    if (time && time !== "000000" && time !== "235959") {
      const at = new Date(2000, 0, 1, Number(time.slice(0, 2)), Number(time.slice(2, 4)));
      text += `, ${at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    }
  }
  if (parts.COUNT) text += `, ${parts.COUNT} times`;
  return text;
}
