import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, Client } from "../src/api.js";

function fakeFetch(status: number, body: unknown, seen: { url?: string; init?: RequestInit } = {}) {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    seen.url = String(url);
    seen.init = init;
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

const input = {
  resource_id: 1, title: "Sync", organizer: "ana", date: "2026-11-02", start_time: "10:00",
  duration_minutes: 30, rrule: null, on_conflict: "reject" as const,
};

test("book sends JSON and the idempotency key", async () => {
  const seen: { url?: string; init?: RequestInit } = {};
  const client = new Client("http://x", fakeFetch(201, { series_id: 7, booked: [], skipped: [] }, seen));
  const res = await client.book(input, "key-123456");
  assert.equal(res.series_id, 7);
  assert.equal(seen.url, "http://x/api/bookings");
  assert.equal((seen.init?.headers as Record<string, string>)["Idempotency-Key"], "key-123456");
  assert.deepEqual(JSON.parse(String(seen.init?.body)), input);
});

test("409 conflicts become readable lines", async () => {
  const details = [{
    recurrence_date: "2026-11-04", local_start: "2026-11-04T13:30+00:00", local_end: "2026-11-04T14:30+00:00",
    note: null,
    problems: [{ kind: "conflict", message: "overlaps an existing booking",
      with: [{ title: "Design review", organizer: "ben", local_start: "2026-11-04T14:00+00:00", local_end: "2026-11-04T15:00+00:00" }] }],
  }, {
    recurrence_date: "2026-11-07", local_start: "", local_end: "", note: null,
    problems: [{ kind: "closed_day", message: "Thames is closed on Saturdays" }],
  }];
  const client = new Client("", fakeFetch(409, { error: "2 of 3 occurrence(s) cannot be booked", details }));
  await assert.rejects(client.book(input, "k-12345678"), (err: unknown) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 409);
    assert.deepEqual(err.lines(), [
      '2026-11-04: clashes with "Design review" (14:00-15:00, ben)',
      "2026-11-07: Thames is closed on Saturdays",
    ]);
    return true;
  });
});

test("pydantic validation errors fall back to a generic message", async () => {
  const client = new Client("", fakeFetch(422, { detail: [{ loc: ["body", "title"], msg: "required" }] }));
  await assert.rejects(client.preview(input), (err: unknown) =>
    err instanceof ApiError && err.message === "Request was rejected" && err.status === 422);
});
