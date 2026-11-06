// Small typed client for the tallyup API. `fetchImpl` is injectable so the
// retry behaviour can be tested without a browser or server.

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  constructor(public status: number, message: string, public body: unknown = null) {
    super(message);
  }
}

export interface Member { id: number; name: string }
export interface Group { id: number; name: string; currency: string; exponent: number; members: Member[] }
export interface SplitPart { member_id: number; value?: string | number | null }
export interface Split { kind: "equal" | "shares" | "percent" | "exact"; parts: SplitPart[] }
export interface ExpenseIn { description: string; payer_id: number; amount: string; split: Split }
export interface Share { member_id: number; amount: string; amount_minor: number }
export interface Expense extends ExpenseIn { id: number; version: number; shares: Share[]; amount_minor: number }
export interface Balance { member_id: number; name: string; balance: string; balance_minor: number; rounding_drift_minor: number }
export interface Transfer { from_member: number; from_name: string; to_member: number; to_name: string; amount: string; amount_minor: number }
export interface SettlePlan { method: string; transfers: Transfer[]; transfer_count: number; greedy_transfer_count: number }

export function newIdempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export interface RetryOptions { attempts?: number; baseDelayMs?: number; sleep?: (ms: number) => Promise<void> }

/**
 * POST with an Idempotency-Key, retrying network failures and 5xx responses
 * with the *same* key, so a retry after a lost response cannot create a
 * duplicate. 4xx responses are returned immediately (retrying will not help).
 */
export async function postIdempotent(
  fetchImpl: FetchLike, url: string, body: unknown, key: string, opts: RetryOptions = {},
): Promise<Response> {
  const attempts = opts.attempts ?? 3;
  const base = opts.baseDelayMs ?? 300;
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let lastErr: unknown = null;
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetchImpl(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify(body),
      });
      if (res.status < 500) return res;
      lastErr = new ApiError(res.status, `server error ${res.status}`);
    } catch (e) {
      lastErr = e;
    }
    if (i < attempts - 1) await sleep(base * 2 ** i);
  }
  throw lastErr;
}

async function unwrap<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d: { msg?: string }) => d.msg ?? String(d)).join("; ")
      : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, data);
  }
  return data as T;
}

export class Client {
  constructor(private fetchImpl: FetchLike = (u, i) => fetch(u, i)) {}

  private async json<T>(url: string, init?: RequestInit): Promise<T> {
    const headers = { "Content-Type": "application/json", ...(init?.headers ?? {}) };
    return unwrap<T>(await this.fetchImpl(url, { ...init, headers }));
  }

  createGroup(name: string, currency: string) {
    return this.json<Group>("/api/groups", { method: "POST", body: JSON.stringify({ name, currency }) });
  }
  group(id: number) { return this.json<Group>(`/api/groups/${id}`); }
  addMember(gid: number, name: string) {
    return this.json<Member>(`/api/groups/${gid}/members`, { method: "POST", body: JSON.stringify({ name }) });
  }
  preview(gid: number, e: ExpenseIn & { expense_id?: number }) {
    return this.json<{ amount_minor: number; shares: Share[] }>(
      `/api/groups/${gid}/preview`, { method: "POST", body: JSON.stringify(e) });
  }
  expenses(gid: number) { return this.json<Expense[]>(`/api/groups/${gid}/expenses`); }
  async createExpense(gid: number, e: ExpenseIn, key: string) {
    return unwrap<Expense>(await postIdempotent(this.fetchImpl, `/api/groups/${gid}/expenses`, e, key));
  }
  updateExpense(id: number, e: ExpenseIn, version: number) {
    return this.json<Expense>(`/api/expenses/${id}`,
      { method: "PUT", body: JSON.stringify(e), headers: { "If-Match": `"${version}"` } });
  }
  deleteExpense(id: number, version: number) {
    return this.json<void>(`/api/expenses/${id}`, { method: "DELETE", headers: { "If-Match": `"${version}"` } });
  }
  balances(gid: number) {
    return this.json<{ currency: string; balances: Balance[]; sum_minor: number }>(`/api/groups/${gid}/balances`);
  }
  settle(gid: number, method: "optimal" | "greedy") {
    return this.json<SettlePlan>(`/api/groups/${gid}/settle?method=${method}`);
  }
  async pay(gid: number, t: { from_member: number; to_member: number; amount: string }, key: string) {
    return unwrap<unknown>(await postIdempotent(this.fetchImpl, `/api/groups/${gid}/payments`, t, key));
  }
}
