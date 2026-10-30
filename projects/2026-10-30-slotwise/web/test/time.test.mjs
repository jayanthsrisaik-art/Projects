import { test } from "node:test";
import assert from "node:assert/strict";
import {
  addDays, groupWeek, localDateKey, localTimeLabel, offsetMs, startOfLocalDay, weekRangeIso, weekStart,
} from "../dist/time.js";

const iso = (ms) => new Date(ms).toISOString();

test("weekStart returns the Monday on or before a date", () => {
  assert.equal(weekStart("2026-10-30"), "2026-10-26"); // Friday
  assert.equal(weekStart("2026-10-26"), "2026-10-26"); // Monday
  assert.equal(weekStart("2026-11-01"), "2026-10-26"); // Sunday
  assert.equal(addDays("2026-12-31", 1), "2027-01-01");
});

test("offsets on both sides of US and EU transitions", () => {
  assert.equal(offsetMs(Date.parse("2026-11-01T05:30:00Z"), "America/New_York"), -4 * 3600e3);
  assert.equal(offsetMs(Date.parse("2026-11-01T06:30:00Z"), "America/New_York"), -5 * 3600e3);
  assert.equal(offsetMs(Date.parse("2026-10-25T00:30:00Z"), "Europe/London"), 3600e3);
  assert.equal(offsetMs(Date.parse("2026-10-25T01:30:00Z"), "Europe/London"), 0);
  assert.equal(offsetMs(Date.parse("2026-10-25T01:30:00Z"), "Asia/Kolkata"), 5.5 * 3600e3);
});

test("startOfLocalDay handles 23h and 25h days", () => {
  assert.equal(iso(startOfLocalDay("2026-03-08", "America/New_York")), "2026-03-08T05:00:00.000Z");
  assert.equal(iso(startOfLocalDay("2026-03-09", "America/New_York")), "2026-03-09T04:00:00.000Z");
  assert.equal(iso(startOfLocalDay("2026-11-01", "America/New_York")), "2026-11-01T04:00:00.000Z");
  assert.equal(iso(startOfLocalDay("2026-11-02", "America/New_York")), "2026-11-02T05:00:00.000Z");
  assert.equal(iso(startOfLocalDay("2026-10-26", "Asia/Tokyo")), "2026-10-25T15:00:00.000Z");
});

test("startOfLocalDay when midnight itself is skipped (America/Santiago spring-forward)", () => {
  // Chile moves 00:00 -> 01:00 on the first Sunday of September 2026.
  const t = startOfLocalDay("2026-09-06", "America/Santiago");
  assert.equal(localDateKey(iso(t), "America/Santiago"), "2026-09-06");
  assert.equal(localTimeLabel(iso(t), "America/Santiago"), "01:00");
  assert.equal(localDateKey(iso(t - 60000), "America/Santiago"), "2026-09-05");
});

test("week range spans 7 local days and is 1h longer across fall-back", () => {
  const r = weekRangeIso("2026-10-26", "America/New_York");
  assert.equal(r.start, "2026-10-26T04:00:00.000Z");
  assert.equal(r.end, "2026-11-02T05:00:00.000Z");
  assert.equal(Date.parse(r.end) - Date.parse(r.start), (7 * 24 + 1) * 3600e3);
});

test("groupWeek places slots by visitor-local date, not UTC date", () => {
  const slots = [
    { start: "2026-10-30T13:00:00Z", end: "2026-10-30T13:30:00Z" }, // Fri 09:00 NY = Fri 22:00 Tokyo
    { start: "2026-10-30T15:30:00Z", end: "2026-10-30T16:00:00Z" }, // Fri 11:30 NY = Sat 00:30 Tokyo
    { start: "2026-10-26T13:00:00Z", end: "2026-10-26T13:30:00Z" },
  ];
  const tokyo = groupWeek(slots, "Asia/Tokyo", "2026-10-26");
  assert.equal(tokyo.length, 7);
  assert.deepEqual(tokyo[4].slots.map((s) => s.label), ["22:00"]);
  assert.deepEqual(tokyo[5].slots.map((s) => s.label), ["00:30"]);
  const ny = groupWeek(slots, "America/New_York", "2026-10-26");
  assert.deepEqual(ny[0].slots.map((s) => s.label), ["09:00"]);
  assert.deepEqual(ny[4].slots.map((s) => s.label), ["09:00", "11:30"]);
});

test("slots outside the week are dropped", () => {
  const cols = groupWeek([{ start: "2026-11-02T14:00:00Z", end: "x" }], "America/New_York", "2026-10-26");
  assert.equal(cols.reduce((n, c) => n + c.slots.length, 0), 0);
});
