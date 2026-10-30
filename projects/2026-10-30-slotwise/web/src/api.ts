import type { Slot } from "./time.js";

export interface EventTypeInfo {
  slug: string;
  title: string;
  host_tz: string;
  duration_min: number;
}

export interface Booking {
  id: string;
  event_type: string;
  start: string;
  end: string;
  invitee_name: string;
  invitee_email: string;
  status: "confirmed" | "cancelled";
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail ?? body;
    const msg = typeof detail === "string" ? detail : detail.message ?? JSON.stringify(detail);
    throw new ApiError(res.status, detail.error ?? "error", msg);
  }
  return body as T;
}

export const api = {
  eventTypes: () => call<EventTypeInfo[]>("/api/event-types"),
  slots: (slug: string, start: string, end: string) =>
    call<{ host_tz: string; slots: Slot[] }>(
      `/api/event-types/${encodeURIComponent(slug)}/slots?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),
  book: (eventType: string, start: string, name: string, email: string, idempotencyKey: string) =>
    call<{ booking: Booking; token: string; replayed: boolean }>("/api/bookings", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ event_type: eventType, start, name, email }),
    }),
  cancel: (id: string, token: string) =>
    call<Booking>(`/api/bookings/${id}/cancel`, { method: "POST", body: JSON.stringify({ token }) }),
};
