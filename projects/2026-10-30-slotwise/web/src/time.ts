// Pure timezone helpers for the visitor's view. The server speaks UTC instants
// only; everything visitor-local (day columns, week boundaries, labels) is
// derived here with Intl, so no timezone database ships with the page.

export interface Slot {
  start: string;
  end: string;
}

export interface LocalParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    formatters.set(tz, f);
  }
  return f;
}

export function isValidTimeZone(tz: string): boolean {
  try {
    formatter(tz);
    return true;
  } catch {
    return false;
  }
}

export function localParts(ms: number, tz: string): LocalParts {
  const out: Record<string, number> = {};
  for (const p of formatter(tz).formatToParts(new Date(ms))) {
    if (p.type !== "literal") out[p.type] = Number(p.value);
  }
  return { year: out.year, month: out.month, day: out.day, hour: out.hour, minute: out.minute };
}

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

export function dateKey(p: { year: number; month: number; day: number }): string {
  return `${pad(p.year, 4)}-${pad(p.month)}-${pad(p.day)}`;
}

export function localDateKey(iso: string, tz: string): string {
  return dateKey(localParts(Date.parse(iso), tz));
}

export function localTimeLabel(iso: string, tz: string): string {
  const p = localParts(Date.parse(iso), tz);
  return `${pad(p.hour)}:${pad(p.minute)}`;
}

/** UTC offset of `tz` at instant `ms`, in milliseconds (local - UTC). */
export function offsetMs(ms: number, tz: string): number {
  const p = localParts(ms, tz);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
  return asUtc - Math.floor(ms / 60000) * 60000;
}

function parseKey(key: string): [number, number, number] {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
  if (!m) throw new Error(`bad date key ${key}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

export function addDays(key: string, n: number): string {
  const [y, m, d] = parseKey(key);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return dateKey({ year: t.getUTCFullYear(), month: t.getUTCMonth() + 1, day: t.getUTCDate() });
}

/** Monday on or before `key` (ISO weeks). */
export function weekStart(key: string): string {
  const [y, m, d] = parseKey(key);
  const dow = (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7; // Mon=0
  return addDays(key, -dow);
}

/** First UTC instant (ms) whose local date in `tz` is `key`. Handles days that
 *  are 23h/25h long and zones where midnight itself falls in a DST gap. */
export function startOfLocalDay(key: string, tz: string): number {
  const [y, m, d] = parseKey(key);
  const wall = Date.UTC(y, m - 1, d);
  // Two fixed-point passes settle the offset across a transition.
  let t = wall - offsetMs(wall, tz);
  t = wall - offsetMs(t, tz);
  if (localDateKey(new Date(t).toISOString(), tz) === key) {
    // Midnight exists, but an earlier instant can still share the date only
    // if t is not the first one; step back to be sure.
    const prev = t - 60000;
    if (localDateKey(new Date(prev).toISOString(), tz) !== key) return t;
  }
  // Midnight is inside a gap (or the guess overshot): bisect on minutes.
  let lo = wall - 15 * 3600000; // definitely the previous local day
  let hi = wall + 15 * 3600000; // definitely this local day or later
  while (hi - lo > 60000) {
    const mid = lo + Math.floor((hi - lo) / 120000) * 60000;
    if (localDateKey(new Date(mid).toISOString(), tz) < key) lo = mid;
    else hi = mid;
  }
  return hi;
}

export interface DayColumn {
  date: string;
  slots: { start: string; label: string }[];
}

/** One column per visitor-local day of the week starting at `week`, with each
 *  slot placed by its visitor-local start date. */
export function groupWeek(slots: Slot[], tz: string, week: string): DayColumn[] {
  const cols: DayColumn[] = [];
  const byDate = new Map<string, DayColumn>();
  for (let i = 0; i < 7; i++) {
    const col = { date: addDays(week, i), slots: [] as DayColumn["slots"] };
    cols.push(col);
    byDate.set(col.date, col);
  }
  const sorted = [...slots].sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  for (const s of sorted) {
    byDate.get(localDateKey(s.start, tz))?.slots.push({ start: s.start, label: localTimeLabel(s.start, tz) });
  }
  return cols;
}

export function weekRangeIso(week: string, tz: string): { start: string; end: string } {
  return {
    start: new Date(startOfLocalDay(week, tz)).toISOString(),
    end: new Date(startOfLocalDay(addDays(week, 7), tz)).toISOString(),
  };
}
