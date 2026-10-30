// Build and describe the RRULE subset the backend accepts.

export const DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
export type DayCode = (typeof DAY_CODES)[number];
const DAY_NAMES: Record<DayCode, string> = {
  MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun",
};

export interface RepeatForm {
  freq: "none" | "DAILY" | "WEEKLY";
  interval: number;
  days: DayCode[];
  end: { kind: "count"; count: number } | { kind: "until"; until: string };
}

export function buildRRule(form: RepeatForm): string | null {
  if (form.freq === "none") return null;
  if (!Number.isInteger(form.interval) || form.interval < 1) {
    throw new Error("Repeat interval must be a whole number of at least 1");
  }
  const parts = [`FREQ=${form.freq}`];
  if (form.interval !== 1) parts.push(`INTERVAL=${form.interval}`);
  if (form.freq === "WEEKLY" && form.days.length > 0) {
    const ordered = DAY_CODES.filter((d) => form.days.includes(d));
    parts.push(`BYDAY=${ordered.join(",")}`);
  }
  if (form.end.kind === "count") {
    if (!Number.isInteger(form.end.count) || form.end.count < 1) {
      throw new Error("Number of occurrences must be at least 1");
    }
    parts.push(`COUNT=${form.end.count}`);
  } else {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(form.end.until)) throw new Error("Pick an end date");
    parts.push(`UNTIL=${form.end.until.replaceAll("-", "")}`);
  }
  return parts.join(";");
}

export function describeRRule(rule: string | null): string {
  if (!rule) return "Does not repeat";
  const fields = new Map(
    rule.split(";").map((p) => p.split("=") as [string, string]),
  );
  const interval = Number(fields.get("INTERVAL") ?? "1");
  const unit = fields.get("FREQ") === "DAILY" ? "day" : "week";
  let text = interval === 1 ? `Every ${unit}` : `Every ${interval} ${unit}s`;
  const byday = fields.get("BYDAY");
  if (byday) {
    text += " on " + byday.split(",").map((d) => DAY_NAMES[d as DayCode] ?? d).join(", ");
  }
  const count = fields.get("COUNT");
  const until = fields.get("UNTIL");
  if (count) text += `, ${count} time${count === "1" ? "" : "s"}`;
  if (until) text += `, until ${until.slice(0, 4)}-${until.slice(4, 6)}-${until.slice(6, 8)}`;
  return text;
}
