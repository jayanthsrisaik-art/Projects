// Browser end-to-end: start the real server, book a slot in a visitor
// timezone, confirm it vanishes, cancel it, and confirm it comes back.
// Run with: npm run e2e   (needs Python deps and Playwright's Chromium)
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PORT = 8765;
const db = join(mkdtempSync(join(tmpdir(), "slotwise-")), "e2e.db");
const server = spawn("python3", ["-m", "slotwise", "--db", db, "--port", String(PORT)], { cwd: ROOT, stdio: "inherit" });

async function waitUp() {
  for (let i = 0; i < 50; i++) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/api/health`)).ok) return; } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("server did not start");
}

let browser;
try {
  await waitUp();
  browser = await chromium.launch();
  const page = await browser.newPage({ timezoneId: "Asia/Tokyo" });
  // Two weeks out so min-notice never interferes, whatever today is.
  const target = new Date(Date.now() + 14 * 86400e3).toISOString().slice(0, 10);
  await page.goto(`http://127.0.0.1:${PORT}/?type=intro-call&tz=Europe/Berlin&week=${target}`);
  await page.waitForSelector("button.slot");
  const label = await page.locator("#week-label").textContent();
  assert.match(label, /Europe\/Berlin/);
  const first = page.locator("button.slot").first();
  const start = await first.getAttribute("data-start");
  const before = await page.locator("button.slot").count();
  await first.click();
  await page.fill("#name", "E2E Tester");
  await page.fill("#email", "e2e@example.com");
  await page.click("#confirm");
  await page.waitForSelector("#confirmation:not([hidden]) h2");
  assert.equal(await page.locator("#confirmation h2").textContent(), "Booked");
  await page.waitForFunction((s) => !document.querySelector(`button.slot[data-start="${s}"]`), start);
  const after = await page.locator("button.slot").count();
  assert.ok(after < before, `slots ${before} -> ${after}`);
  // The .ics link serves a calendar file.
  const href = await page.locator("#confirmation a").getAttribute("href");
  const ics = await (await fetch(`http://127.0.0.1:${PORT}${href}`)).text();
  assert.match(ics, /BEGIN:VEVENT/);
  await page.click("#cancel-btn");
  await page.waitForFunction(() => document.querySelector("#confirmation h2")?.textContent === "Cancelled");
  await page.waitForSelector(`button.slot[data-start="${start}"]`);
  console.log(`e2e ok: booked ${start} from Europe/Berlin view, slots ${before} -> ${after} -> restored`);
} finally {
  await browser?.close();
  server.kill();
}
