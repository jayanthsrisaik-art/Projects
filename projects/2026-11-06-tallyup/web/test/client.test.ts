import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { MoneyError, formatAmount, parseAmount } from "../src/money.js";
import { ApiError, postIdempotent, type FetchLike } from "../src/api.js";

// dist/test/client.test.js -> project root/samples
const vectorsPath = fileURLToPath(new URL("../../../samples/money_vectors.json", import.meta.url));
const vectors: { text: string; exponent: number; minor?: number; error?: boolean; allow_zero?: boolean }[] =
  JSON.parse(readFileSync(vectorsPath, "utf8"));

test("parseAmount matches the shared vectors used by the Python tests", () => {
  assert.ok(vectors.length >= 15);
  for (const v of vectors) {
    if (v.error) {
      assert.throws(() => parseAmount(v.text, v.exponent, { allowZero: v.allow_zero }), MoneyError, v.text);
    } else {
      assert.equal(parseAmount(v.text, v.exponent, { allowZero: v.allow_zero }), v.minor, v.text);
    }
  }
});

test("formatAmount round-trips parseAmount", () => {
  for (const [minor, exp, text] of [[1230, 2, "12.30"], [5, 2, "0.05"], [1500, 0, "1500"], [1, 3, "0.001"], [-250, 2, "-2.50"]] as const) {
    assert.equal(formatAmount(minor, exp), text);
    assert.equal(parseAmount(text, exp, { allowNegative: true }), minor);
  }
});

function fakeResponse(status: number): Response {
  return new Response(status === 204 ? null : JSON.stringify({ status }), { status });
}

test("postIdempotent retries network errors and 5xx with the same key", async () => {
  const seenKeys: string[] = [];
  const outcomes: (number | "throw")[] = ["throw", 503, 201];
  const f: FetchLike = async (_url, init) => {
    seenKeys.push((init!.headers as Record<string, string>)["Idempotency-Key"]);
    const o = outcomes.shift()!;
    if (o === "throw") throw new TypeError("network down");
    return fakeResponse(o);
  };
  const res = await postIdempotent(f, "/x", { a: 1 }, "key-1", { attempts: 3, sleep: async () => {} });
  assert.equal(res.status, 201);
  assert.deepEqual(seenKeys, ["key-1", "key-1", "key-1"]);
});

test("postIdempotent does not retry 4xx", async () => {
  let calls = 0;
  const f: FetchLike = async () => { calls++; return fakeResponse(422); };
  const res = await postIdempotent(f, "/x", {}, "k", { sleep: async () => {} });
  assert.equal(res.status, 422);
  assert.equal(calls, 1);
});

test("postIdempotent gives up after the attempt budget", async () => {
  let calls = 0;
  const f: FetchLike = async () => { calls++; return fakeResponse(500); };
  await assert.rejects(postIdempotent(f, "/x", {}, "k", { attempts: 2, sleep: async () => {} }), ApiError);
  assert.equal(calls, 2);
});
