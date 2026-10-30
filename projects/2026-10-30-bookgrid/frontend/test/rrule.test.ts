import assert from "node:assert/strict";
import { test } from "node:test";

import { buildRRule, describeRRule } from "../src/rrule.js";

test("no repeat gives null", () => {
  assert.equal(buildRRule({ freq: "none", interval: 1, days: [], end: { kind: "count", count: 3 } }), null);
});

test("weekly rule orders days Monday-first and omits INTERVAL=1", () => {
  const rule = buildRRule({ freq: "WEEKLY", interval: 1, days: ["FR", "MO", "WE"], end: { kind: "count", count: 6 } });
  assert.equal(rule, "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=6");
});

test("BYDAY is dropped for daily rules and UNTIL is compacted", () => {
  const rule = buildRRule({ freq: "DAILY", interval: 2, days: ["MO"], end: { kind: "until", until: "2026-12-31" } });
  assert.equal(rule, "FREQ=DAILY;INTERVAL=2;UNTIL=20261231");
});

test("invalid inputs throw readable errors", () => {
  assert.throws(() => buildRRule({ freq: "DAILY", interval: 0, days: [], end: { kind: "count", count: 2 } }), /interval/);
  assert.throws(() => buildRRule({ freq: "DAILY", interval: 1, days: [], end: { kind: "count", count: 0 } }), /at least 1/);
  assert.throws(() => buildRRule({ freq: "DAILY", interval: 1, days: [], end: { kind: "until", until: "" } }), /end date/);
});

test("describe rules in plain English", () => {
  assert.equal(describeRRule(null), "Does not repeat");
  assert.equal(describeRRule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=6"), "Every 2 weeks on Mon, Wed, 6 times");
  assert.equal(describeRRule("FREQ=DAILY;COUNT=1"), "Every day, 1 time");
  assert.equal(describeRRule("FREQ=WEEKLY;UNTIL=20261130"), "Every week, until 2026-11-30");
});
