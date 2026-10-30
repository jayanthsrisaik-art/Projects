// Typed client for the bookgrid API.

export interface Resource {
  id: number;
  name: string;
  timezone: string;
  open_time: string;
  close_time: string;
  open_days: string[];
  buffer_minutes: number;
}

export interface Occurrence {
  id: number;
  series_id: number;
  title: string;
  organizer: string;
  rrule: string | null;
  recurrence_date: string;
  local_start: string;
  local_end: string;
  start_utc: string;
  status: "active" | "cancelled";
  moved: boolean;
  note: string | null;
}

export interface Problem {
  kind: "conflict" | "outside_hours" | "closed_day" | "self_overlap";
  message: string;
  with?: { title: string; organizer: string; local_start: string; local_end: string }[];
}

export interface PlannedOccurrence {
  recurrence_date: string;
  local_start: string;
  local_end: string;
  note: string | null;
  problems?: Problem[];
}

export interface BookingInput {
  resource_id: number;
  title: string;
  organizer: string;
  date: string;
  start_time: string;
  duration_minutes: number;
  rrule: string | null;
  on_conflict: "reject" | "skip";
}

export interface BookingResult {
  series_id: number;
  booked: (PlannedOccurrence & { id: number })[];
  skipped: PlannedOccurrence[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public details: unknown) {
    super(message);
  }

  /** One readable line per blocked occurrence, for showing under the form. */
  lines(): string[] {
    if (Array.isArray(this.details)) {
      return this.details.map((d) => {
        if (d && typeof d === "object" && "problems" in d) {
          const occ = d as PlannedOccurrence;
          return `${occ.recurrence_date}: ${describeProblems(occ.problems ?? [])}`;
        }
        if (d && typeof d === "object" && "message" in d) {
          return describeProblems([d as Problem]);
        }
        return String(d);
      });
    }
    return [];
  }
}

export function describeProblems(problems: Problem[]): string {
  return problems
    .map((p) => {
      if (p.kind === "conflict" && p.with?.length) {
        const w = p.with[0];
        return `clashes with "${w.title}" (${w.local_start.slice(11, 16)}-${w.local_end.slice(11, 16)}, ${w.organizer})`;
      }
      return p.message;
    })
    .join("; ");
}

type Fetch = typeof fetch;

export class Client {
  constructor(private base = "", private fetchImpl: Fetch = (...a) => fetch(...a)) {}

  private async request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
    const res = await this.fetchImpl(this.base + path, {
      method,
      headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // Our errors are {error, details}; FastAPI/pydantic ones are {detail}.
      const message = data.error ?? (typeof data.detail === "string" ? data.detail : "Request was rejected");
      throw new ApiError(res.status, message, data.details ?? data.detail);
    }
    return data as T;
  }

  resources() {
    return this.request<Resource[]>("GET", "/api/resources");
  }
  occurrences(rid: number, start: string, end: string) {
    return this.request<Occurrence[]>("GET", `/api/resources/${rid}/occurrences?start=${start}&end=${end}`);
  }
  free(rid: number, date: string, duration: number, step = 15) {
    return this.request<{ slots: { local_start: string; local_end: string }[]; closed: boolean }>(
      "GET", `/api/resources/${rid}/free?date=${date}&duration=${duration}&step=${step}`);
  }
  preview(input: BookingInput) {
    return this.request<{ occurrences: PlannedOccurrence[]; bookable: number; blocked: number }>(
      "POST", "/api/bookings/preview", input);
  }
  book(input: BookingInput, idempotencyKey: string) {
    return this.request<BookingResult>("POST", "/api/bookings", input, { "Idempotency-Key": idempotencyKey });
  }
  move(oid: number, date: string, startTime: string) {
    return this.request<Occurrence>("PATCH", `/api/occurrences/${oid}`, { date, start_time: startTime });
  }
  cancel(oid: number, scope: "this" | "following" | "series") {
    return this.request<{ cancelled: number }>("POST", `/api/occurrences/${oid}/cancel`, { scope });
  }
}
