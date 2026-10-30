import assert from "node:assert/strict";
import { test } from "node:test";

import { addDays, mondayOf, parseLocal, place, snapToGrid, weekDates } from "../src/grid.js";

test("week helpers cross month and year boundaries", () => {
  assert.equal(mondayOf("2026-11-01"), "2026-10-26"); // Sunday
  assert.equal(mondayOf("2026-11-02"), "2026-11-02");
  assert.deepEqual(weekDates("2026-12-31").slice(0, 1).concat(weekDates("2026-12-31").slice(-1)),
    ["2026-12-28", "2027-01-03"]);
  assert.equal(addDays("2027-03-13", 1), "2027-03-14");
});

test("parseLocal reads the room's wall clock, not the browser's", () => {
  // Same instant, different rooms: the grid must use the local digits.
  assert.deepEqual(parseLocal("2026-11-02T11:00-05:00"), { date: "2026-11-02", minutes: 660 });
  assert.deepEqual(parseLocal("2026-11-02T16:00+00:00"), { date: "2026-11-02", minutes: 960 });
  assert.throws(() => parseLocal("tomorrow"));
});

test("place positions within the visible day", () => {
  const p = place("2026-11-04T10:00+00:00", "2026-11-04T11:30+00:00", "2026-11-02", 8 * 60, 18 * 60);
  assert.deepEqual(p, { dayIndex: 2, topPct: 20, heightPct: 15, clippedStart: false, clippedEnd: false });
});

test("place clips bookings that start before the grid or run past midnight", () => {
  const early = place("2026-11-02T07:00+00:00", "2026-11-02T09:00+00:00", "2026-11-02", 480, 1080);
  assert.equal(early?.topPct, 0);
  assert.equal(early?.clippedStart, true);
  const late = place("2026-11-02T23:30-05:00", "2026-11-03T00:30-05:00", "2026-11-02", 0, 1440);
  assert.equal(late?.clippedEnd, false);
  assert.ok(Math.abs((late?.heightPct ?? 0) - (30 / 1440) * 100) < 1e-9);
});

test("place ignores bookings outside the week or entirely outside the hours", () => {
  assert.equal(place("2026-11-09T10:00+00:00", "2026-11-09T11:00+00:00", "2026-11-02", 480, 1080), null);
  assert.equal(place("2026-11-02T06:00+00:00", "2026-11-02T07:00+00:00", "2026-11-02", 480, 1080), null);
});

test("snapToGrid floors to the step and never starts at closing time", () => {
  assert.equal(snapToGrid(0, 480, 1080, 15), "08:00");
  assert.equal(snapToGrid(0.26, 480, 1080, 15), "10:30"); // 08:00 + 156 min = 10:36
  assert.equal(snapToGrid(1, 480, 1080, 15), "17:45");
  assert.equal(snapToGrid(1.4, 480, 1080, 15), "17:45");
});
