// Client-side flag evaluation. Mirrors flagdeck/evaluator.py; samples/parity_expected.json
// pins both implementations to identical results.

export const BUCKET_SCALE = 100_000;

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Context = { key: string; [attr: string]: Json };

export interface Clause { attribute: string; op: string; values: Json[]; negate?: boolean }
export interface Serve { variation?: number | null; rollout?: { bucket_by?: string; weights: number[] } | null }
export interface Rule { id: string; description?: string; clauses: Clause[]; serve: Serve }
export interface EnvConfig {
  on: boolean;
  off_variation: number;
  targets: { variation: number; values: string[] }[];
  rules: Rule[];
  fallthrough: Serve;
}
export interface Flag {
  key: string;
  description?: string;
  variations: Json[];
  salt?: string;
  prerequisites?: { key: string; variation: number }[];
  environments: Record<string, EnvConfig>;
}
export interface Reason { kind: string; error?: string; prerequisite?: string; rule_index?: number; rule_id?: string }
export interface Result { value: Json; variation: number | null; reason: Reason }
export type Lookup = (key: string) => Flag | undefined;

// ---------------------------------------------------------------------------- hashing
export function murmur3_32(data: Uint8Array, seed = 0): number {
  const c1 = 0xcc9e2d51, c2 = 0x1b873593;
  let h = seed >>> 0;
  const n = data.length;
  const nblocks = n >>> 2;
  for (let i = 0; i < nblocks; i++) {
    const o = i * 4;
    let k = data[o] | (data[o + 1] << 8) | (data[o + 2] << 16) | (data[o + 3] << 24);
    k = Math.imul(k, c1);
    k = (k << 15) | (k >>> 17);
    k = Math.imul(k, c2);
    h ^= k;
    h = (h << 13) | (h >>> 19);
    h = (Math.imul(h, 5) + 0xe6546b64) | 0;
  }
  const t = nblocks * 4;
  const rem = n & 3;
  let k = 0;
  if (rem >= 3) k ^= data[t + 2] << 16;
  if (rem >= 2) k ^= data[t + 1] << 8;
  if (rem >= 1) {
    k ^= data[t];
    k = Math.imul(k, c1);
    k = (k << 15) | (k >>> 17);
    k = Math.imul(k, c2);
    h ^= k;
  }
  h ^= n;
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return h >>> 0;
}

const encoder = new TextEncoder();

export function bucketKey(value: Json | undefined): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  return null;
}

export function bucket(flagKey: string, salt: string, value: string): number {
  return murmur3_32(encoder.encode(`${flagKey}.${salt}.${value}`)) % BUCKET_SCALE;
}

// ---------------------------------------------------------------------------- clauses
function kind(v: Json | undefined): string {
  if (v === null || v === undefined) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v === "object" ? "object" : typeof v; // boolean | number | string
}

export function jsonEqual(a: Json, b: Json): boolean {
  const ka = kind(a);
  if (ka !== kind(b)) return false;
  if (ka === "array") {
    const x = a as Json[], y = b as Json[];
    return x.length === y.length && x.every((v, i) => jsonEqual(v, y[i]));
  }
  if (ka === "object") {
    const x = a as Record<string, Json>, y = b as Record<string, Json>;
    const kx = Object.keys(x), ky = Object.keys(y);
    return kx.length === ky.length && kx.every((k) => k in y && jsonEqual(x[k], y[k]));
  }
  return a === b;
}

function matchOne(op: string, actual: Json, expected: Json): boolean {
  switch (op) {
    case "in":
      return jsonEqual(actual, expected);
    case "startsWith":
    case "endsWith":
    case "contains":
    case "matches": {
      if (typeof actual !== "string" || typeof expected !== "string") return false;
      if (op === "startsWith") return actual.startsWith(expected);
      if (op === "endsWith") return actual.endsWith(expected);
      if (op === "contains") return actual.includes(expected);
      try {
        return new RegExp(expected, "u").test(actual);
      } catch {
        return false;
      }
    }
    case "lt":
    case "lte":
    case "gt":
    case "gte": {
      if (typeof actual !== "number" || typeof expected !== "number") return false;
      if (op === "lt") return actual < expected;
      if (op === "lte") return actual <= expected;
      if (op === "gt") return actual > expected;
      return actual >= expected;
    }
    default:
      return false;
  }
}

export function clauseMatches(clause: Clause, context: Context): boolean {
  const actual = context[clause.attribute];
  // Missing attributes never match, even when negated (same as the server).
  if (actual === null || actual === undefined) return false;
  const candidates = Array.isArray(actual) ? actual : [actual];
  const hit = candidates.some((c) => clause.values.some((e) => matchOne(clause.op, c, e)));
  return clause.negate ? !hit : hit;
}

// ---------------------------------------------------------------------------- evaluation
function result(flag: Flag, index: number, reason: Reason): Result {
  return { value: flag.variations[index], variation: index, reason };
}

function error(kind: string): Result {
  return { value: null, variation: null, reason: { kind: "ERROR", error: kind } };
}

function serve(flag: Flag, s: Serve, context: Context): number {
  if (s.variation !== null && s.variation !== undefined) return s.variation;
  const rollout = s.rollout!;
  const value = bucketKey(context[rollout.bucket_by || "key"]);
  const b = value === null ? 0 : bucket(flag.key, flag.salt || flag.key, value);
  let cumulative = 0;
  let lastNonzero = 0;
  for (let i = 0; i < rollout.weights.length; i++) {
    if (rollout.weights[i] > 0) lastNonzero = i;
    cumulative += rollout.weights[i];
    if (b < cumulative) return i;
  }
  return lastNonzero;
}

export function evaluate(flag: Flag, env: string, context: Context, lookup: Lookup, stack: string[] = []): Result {
  const key = context.key;
  if (typeof key !== "string" || key === "") return error("CONTEXT_KEY_MISSING");
  const cfg = flag.environments[env];
  if (!cfg) return error("UNKNOWN_ENVIRONMENT");
  if (stack.includes(flag.key)) return error("PREREQUISITE_CYCLE");

  if (!cfg.on) return result(flag, cfg.off_variation, { kind: "OFF" });

  for (const pre of flag.prerequisites ?? []) {
    const preFlag = lookup(pre.key);
    let ok = false;
    if (preFlag) {
      const preCfg = preFlag.environments[env];
      const res = evaluate(preFlag, env, context, lookup, [...stack, flag.key]);
      if (res.reason.error === "PREREQUISITE_CYCLE") return res;
      ok = Boolean(preCfg && preCfg.on) && res.variation === pre.variation;
    }
    if (!ok) return result(flag, cfg.off_variation, { kind: "PREREQUISITE_FAILED", prerequisite: pre.key });
  }

  for (const target of cfg.targets ?? []) {
    if (target.values.includes(key)) return result(flag, target.variation, { kind: "TARGET_MATCH" });
  }

  const rules = cfg.rules ?? [];
  for (let i = 0; i < rules.length; i++) {
    if (rules[i].clauses.every((c) => clauseMatches(c, context))) {
      return result(flag, serve(flag, rules[i].serve, context), { kind: "RULE_MATCH", rule_index: i, rule_id: rules[i].id });
    }
  }
  return result(flag, serve(flag, cfg.fallthrough, context), { kind: "FALLTHROUGH" });
}

export function evaluateAll(flags: Record<string, Flag>, env: string, context: Context): Record<string, Result> {
  const out: Record<string, Result> = {};
  for (const k of Object.keys(flags).sort()) out[k] = evaluate(flags[k], env, context, (x) => flags[x]);
  return out;
}
