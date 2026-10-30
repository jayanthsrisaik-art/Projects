// End-to-end: start the real server on a temp DB, drive the UI in headless Chromium.
// Run with `npm run e2e` (needs Playwright + Chromium; set PYTHON if `python3` lacks the deps).
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const PORT = 8000 + Math.floor(Math.random() * 1000);
const BASE = `http://127.0.0.1:${PORT}`;
const dir = mkdtempSync(join(tmpdir(), "flagdeck-e2e-"));

const server = spawn(process.env.PYTHON || "python3",
  ["-m", "flagdeck", "--db", join(dir, "e2e.db"), "--port", String(PORT), "--seed", "samples/flags.json"],
  { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] });

async function waitForServer() {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${BASE}/api/meta`)).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error("server did not start");
}

const step = (msg) => console.log(`  ✓ ${msg}`);

async function main() {
  await waitForServer();
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("dialog", (d) => d.accept());
  const errors = [];
  page.on("pageerror", (e) => errors.push(e));
  await page.goto(BASE);

  const status = page.getByTestId("status");
  await page.getByTestId("flag-new-checkout").waitFor();
  step("flag list renders seeded flags");

  await page.getByTestId("toggle-checkout-banner").click();
  await expectText(status, "checkout-banner turned off in prod");
  let banner = await (await fetch(`${BASE}/api/flags/checkout-banner`)).json();
  assert.equal(banner.flag.environments.prod.on, false);
  step("kill switch toggles prod and persists");

  await page.getByTestId("flag-new-checkout").click();
  await page.waitForFunction(() => document.querySelector("#editor-title").textContent.startsWith("new-checkout"));
  const editor = page.getByTestId("editor-json");
  const doc = JSON.parse(await editor.inputValue());
  doc.environments.prod.fallthrough = { rollout: { bucket_by: "key", weights: [50000, 50000] } };
  await editor.fill(JSON.stringify(doc, null, 2));
  await page.getByTestId("preview").click();
  const summary = await page.getByTestId("preview-summary").textContent();
  assert.match(summary, /^\d+ of 5000 synthetic prod users \([\d.]+%\) would change variation\.$/);
  step(`impact preview: "${summary}"`);

  await page.getByTestId("comment").fill("ramp to 50%");
  await page.getByTestId("save").click();
  await expectText(status, "Saved v2");
  step("save with If-Match succeeds (v2)");

  // Someone else edits the flag behind our back...
  const latest = await (await fetch(`${BASE}/api/flags/new-checkout`)).json();
  const res = await fetch(`${BASE}/api/flags/new-checkout`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "If-Match": `"${latest.version}"`, "X-Actor": "teammate" },
    body: JSON.stringify({ ...latest.flag, description: "edited in another tab" }),
  });
  assert.equal(res.status, 200);
  // ...then we save our stale draft.
  const mine = JSON.parse(await editor.inputValue());
  mine.description = "my description";
  await editor.fill(JSON.stringify(mine, null, 2));
  await page.getByTestId("save").click();
  await page.getByTestId("conflict").waitFor({ state: "visible" });
  await expectText(status, "Save rejected: version conflict");
  step("stale save is rejected with a conflict banner (412)");

  await page.getByTestId("overwrite").click();
  await expectText(status, "Saved v4");
  const after = await (await fetch(`${BASE}/api/flags/new-checkout`)).json();
  assert.equal(after.flag.description, "my description");
  step("explicit overwrite after conflict saves v4");

  await page.getByRole("button", { name: "Audit log" }).click();
  await page.getByTestId("audit-entry").first().waitFor();
  assert.equal(await page.getByTestId("audit-entry").count(), 4);
  const head = await page.getByTestId("audit-entry").first().textContent();
  assert.match(head, /v4 · update · me/);
  step("audit log shows 4 versions with actors and diffs");

  await page.getByRole("button", { name: "Playground" }).click();
  await page.getByTestId("run-eval").click();
  await page.getByTestId("eval-new-checkout").waitFor();
  const rows = await page.locator("#playground-out tbody tr").allTextContents();
  assert.equal(rows.length, 4);
  for (const r of rows) assert.ok(r.endsWith("match") && !r.includes("MISMATCH"), r);
  assert.match(await page.getByTestId("eval-new-checkout").textContent(), /RULE_MATCH \(staff\)/);
  step("playground: server and local TS SDK agree on all 4 flags");

  await page.getByRole("button", { name: "Editor" }).click();
  await page.getByTestId("new-flag").click();
  const tpl = JSON.parse(await editor.inputValue());
  tpl.key = "e2e-flag";
  tpl.environments.prod.fallthrough = { rollout: { weights: [1, 2] } };
  await editor.fill(JSON.stringify(tpl));
  await page.getByTestId("save").click();
  await expectText(status, "sum to 3, expected 100000");
  tpl.environments.prod.fallthrough = { variation: 1 };
  await editor.fill(JSON.stringify(tpl));
  await page.getByTestId("save").click();
  await page.getByTestId("flag-e2e-flag").waitFor();
  step("create flow surfaces validation errors, then creates the flag");

  assert.deepEqual(errors, []);
  await browser.close();
}

async function expectText(locator, text) {
  for (let i = 0; i < 50; i++) {
    if ((await locator.textContent())?.includes(text)) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`expected "${text}", got "${await locator.textContent()}"`);
}

let code = 0;
try {
  await main();
  console.log("e2e passed");
} catch (e) {
  console.error(e);
  code = 1;
} finally {
  server.kill();
  rmSync(dir, { recursive: true, force: true });
}
process.exit(code);
