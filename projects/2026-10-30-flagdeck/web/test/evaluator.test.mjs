// Run with `npm test` (builds first). Uses only node:test, no extra dependencies.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { murmur3_32, evaluate, evaluateAll, bucket, jsonEqual, clauseMatches } from "../dist/evaluator.js";
import { FlagClient } from "../dist/client.js";

const samples = (name) => JSON.parse(readFileSync(fileURLToPath(new URL(`../../samples/${name}`, import.meta.url)), "utf8"));
const enc = (s) => new TextEncoder().encode(s);

// Python's FlagIn fills in defaults; replicate the few the samples rely on.
function normalise(doc) {
  const envs = {};
  for (const env of ["dev", "prod"]) {
    const e = doc.environments?.[env] ?? {};
    envs[env] = { on: e.on ?? false, off_variation: e.off_variation ?? 0, targets: e.targets ?? [],
                  rules: e.rules ?? [], fallthrough: e.fallthrough ?? { variation: 0 } };
  }
  return { ...doc, salt: doc.salt || doc.key, prerequisites: doc.prerequisites ?? [], environments: envs };
}

test("murmur3_32 matches reference vectors", () => {
  assert.equal(murmur3_32(enc(""), 0), 0);
  assert.equal(murmur3_32(enc("hello"), 0), 0x248bfa47);
  assert.equal(murmur3_32(enc("Hello, world!"), 1234), 0xfaf6cdb3);
  assert.equal(murmur3_32(enc("The quick brown fox jumps over the lazy dog"), 0), 0x2e4ff723);
  assert.equal(murmur3_32(enc("flag.salt.ü"), 0), 0x5d46f11e); // multi-byte UTF-8 tail, value from the Python impl
});

test("evaluates samples identically to the Python server (parity file)", () => {
  const flags = Object.fromEntries(samples("flags.json").map((d) => [d.key, normalise(d)]));
  const contexts = samples("contexts.json");
  const expected = samples("parity_expected.json");
  let checked = 0;
  for (const env of ["dev", "prod"]) {
    for (const ctx of contexts) {
      const got = JSON.parse(JSON.stringify(evaluateAll(flags, env, ctx)));
      assert.deepEqual(got, expected[env][ctx.key], `${env}/${ctx.key}`);
      checked += Object.keys(got).length;
    }
  }
  assert.equal(checked, 2 * contexts.length * Object.keys(flags).length);
});

test("raising a rollout percentage never removes users who already had the feature", () => {
  const mk = (pct) => normalise({ key: "f", variations: [false, true],
    environments: { prod: { on: true, fallthrough: { rollout: { weights: [100000 - pct, pct] } } } } });
  const on = (flag) => new Set([...Array(5000).keys()].filter((i) => evaluate(flag, "prod", { key: `u${i}` }, () => undefined).value === true));
  const at10 = on(mk(10000)), at25 = on(mk(25000));
  for (const u of at10) assert.ok(at25.has(u));
  assert.ok(Math.abs(at25.size / 5000 - 0.25) < 0.03, `observed ${at25.size / 5000}`);
});

test("strict JSON equality and missing attributes", () => {
  assert.equal(jsonEqual(true, 1), false);
  assert.equal(jsonEqual(1, 1.0), true);
  assert.equal(jsonEqual({ a: [1, "x"] }, { a: [1, "x"] }), true);
  const notUS = { attribute: "country", op: "in", values: ["US"], negate: true };
  assert.equal(clauseMatches(notUS, { key: "a" }), false);
  assert.equal(clauseMatches(notUS, { key: "a", country: "DE" }), true);
  assert.ok(bucket("f", "f", "u1") < 100000);
});

test("FlagClient uses If-None-Match and falls back on unknown flags", async () => {
  const payload = { env: "prod", flags: { f: normalise({ key: "f", variations: ["a", "b"], environments: { prod: { on: true, fallthrough: { variation: 1 } } } }) } };
  const seen = [];
  const fakeFetch = async (url, init) => {
    seen.push(init.headers["If-None-Match"] ?? null);
    if (init.headers["If-None-Match"] === '"t1"') return new Response(null, { status: 304 });
    return new Response(JSON.stringify(payload), { status: 200, headers: { ETag: '"t1"' } });
  };
  const c = new FlagClient("http://x", "prod", fakeFetch);
  assert.equal(await c.refresh(), true);
  assert.equal(await c.refresh(), false);
  assert.deepEqual(seen, [null, '"t1"']);
  assert.equal(c.variation("f", { key: "u" }, "fallback"), "b");
  assert.equal(c.variation("missing", { key: "u" }, "fallback"), "fallback");
});
