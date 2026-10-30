// Minimal SDK: fetch the ruleset for one environment once, then evaluate locally with no
// network round-trip per flag. refresh() uses If-None-Match so unchanged polls cost a 304.
import { Context, Flag, Json, Result, evaluate } from "./evaluator.js";

export interface SdkPayload { env: string; flags: Record<string, Flag> }

export class FlagClient {
  private flags: Record<string, Flag> = {};
  private etag: string | null = null;
  env: string;

  // Wrap the global so it is never invoked with the client as `this` ("Illegal invocation" in browsers).
  constructor(private baseUrl: string, env: string, private fetchImpl: typeof fetch = (input, init) => fetch(input, init)) {
    this.env = env;
  }

  /** Returns true when the ruleset changed. */
  async refresh(): Promise<boolean> {
    const headers: Record<string, string> = {};
    if (this.etag) headers["If-None-Match"] = this.etag;
    const res = await this.fetchImpl(`${this.baseUrl}/api/sdk/${encodeURIComponent(this.env)}`, { headers });
    if (res.status === 304) return false;
    if (!res.ok) throw new Error(`sdk payload: HTTP ${res.status}`);
    const payload = (await res.json()) as SdkPayload;
    this.flags = payload.flags;
    this.etag = res.headers.get("ETag");
    return true;
  }

  load(payload: SdkPayload): void {
    this.env = payload.env;
    this.flags = payload.flags;
  }

  detail(key: string, context: Context): Result {
    const flag = this.flags[key];
    if (!flag) return { value: null, variation: null, reason: { kind: "ERROR", error: "FLAG_NOT_FOUND" } };
    return evaluate(flag, this.env, context, (k) => this.flags[k]);
  }

  /** Value, or ``fallback`` when the flag is unknown or evaluation errored. */
  variation<T extends Json>(key: string, context: Context, fallback: T): T {
    const r = this.detail(key, context);
    return r.reason.kind === "ERROR" ? fallback : (r.value as T);
  }

  keys(): string[] {
    return Object.keys(this.flags).sort();
  }
}
