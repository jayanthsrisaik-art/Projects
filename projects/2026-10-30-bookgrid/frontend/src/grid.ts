// Pure helpers for the week grid. All times are the *room's* wall-clock
// times as returned by the API (e.g. "2026-11-02T10:00-05:00"), so a room in
// New York renders in New York time whatever the browser's timezone is.

export interface Placed {
  dayIndex: number; // 0 = Monday of the shown week
  topPct: number;
  heightPct: number;
  clippedStart: boolean;
  clippedEnd: boolean;
}

export function parseLocal(iso: string): { date: string; minutes: number } {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) throw new Error(`not a local ISO timestamp: ${iso}`);
  return { date: m[1], minutes: Number(m[2]) * 60 + Number(m[3]) };
}

export function hhmmToMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

export function minutesToHhmm(total: number): string {
  const h = Math.floor(total / 60);
  const m = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// Calendar-date arithmetic done in UTC so the browser's own DST never shifts a day.
export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function mondayOf(date: string): string {
  const dow = new Date(`${date}T00:00:00Z`).getUTCDay(); // 0 = Sunday
  return addDays(date, -((dow + 6) % 7));
}

export function weekDates(date: string): string[] {
  const monday = mondayOf(date);
  return Array.from({ length: 7 }, (_, i) => addDays(monday, i));
}

/** Position a booking in a column spanning [dayStart, dayEnd) minutes. */
export function place(
  localStart: string,
  localEnd: string,
  monday: string,
  dayStart: number,
  dayEnd: number,
): Placed | null {
  const s = parseLocal(localStart);
  const e = parseLocal(localEnd);
  const dayIndex = weekDates(monday).indexOf(s.date);
  if (dayIndex < 0) return null;
  // A booking that ends at or after local midnight is drawn to the bottom.
  const endMin = e.date === s.date ? e.minutes : 24 * 60;
  const span = dayEnd - dayStart;
  const top = Math.max(s.minutes, dayStart);
  const bottom = Math.min(endMin, dayEnd);
  if (bottom <= top) return null;
  return {
    dayIndex,
    topPct: ((top - dayStart) / span) * 100,
    heightPct: ((bottom - top) / span) * 100,
    clippedStart: s.minutes < dayStart,
    clippedEnd: endMin > dayEnd,
  };
}

/** Turn a click position (0..1 down the column) into a start time on a step grid. */
export function snapToGrid(fraction: number, dayStart: number, dayEnd: number, step: number): string {
  const raw = dayStart + Math.max(0, Math.min(1, fraction)) * (dayEnd - dayStart);
  const snapped = Math.floor(raw / step) * step;
  return minutesToHhmm(Math.min(snapped, dayEnd - step));
}
