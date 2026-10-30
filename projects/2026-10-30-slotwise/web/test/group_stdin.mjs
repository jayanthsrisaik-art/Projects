// Reads [{tz, week, slots}] on stdin, prints the TS view of each case so the
// Python suite can compare it with zoneinfo.
import { groupWeek, weekRangeIso } from "../dist/time.js";

let input = "";
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const out = JSON.parse(input).map(({ tz, week, slots }) => ({
    range: weekRangeIso(week, tz),
    cols: groupWeek(slots, tz, week).map((c) => ({ date: c.date, labels: c.slots.map((s) => s.label) })),
  }));
  process.stdout.write(JSON.stringify(out));
});
