// flagdeck admin UI: flag list with per-environment kill switches, JSON editor with
// optimistic-concurrency saves, blast-radius preview, audit trail and an evaluation
// playground that cross-checks the server against the local TypeScript evaluator.
import { FlagClient } from "./client.js";
import { Context, Flag, Json, Result, jsonEqual } from "./evaluator.js";

interface Row { version: number; updated_at: string; flag: Flag }
interface Change { path: string; op: string; before: unknown; after: unknown }
interface AuditEntry { version: number; action: string; actor: string; comment: string; at: string; diff: Change[] }
interface Preview { total: number; changed: number; before: Record<string, number>; after: Record<string, number>;
  examples: { context: Context; before: Result; after: Result }[] }

class HttpError extends Error {
  constructor(public status: number, public body: any) {
    super(typeof body?.detail === "string" ? body.detail : Array.isArray(body?.detail) ? body.detail.join("\n") : `HTTP ${status}`);
  }
}

const state = {
  env: "prod",
  actor: readActor(),
  rows: [] as Row[],
  selected: null as string | null, // flag key, or null for "new flag"
  loadedVersion: 0,
  tab: "editor" as "editor" | "audit" | "playground",
};

function readActor(): string {
  try { return localStorage.getItem("flagdeck.actor") || "me"; } catch { return "me"; }
}

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;

function h(tag: string, attrs: Record<string, string> = {}, ...children: (Node | string)[]): HTMLElement {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...children);
  return el;
}

async function api<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ data: T; etag: string | null }> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Actor": state.actor, ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new HttpError(res.status, data);
  return { data: data as T, etag: res.headers.get("ETag") };
}

function setStatus(msg: string, kind: "ok" | "error" | "" = ""): void {
  const el = $("#status");
  el.textContent = msg;
  el.className = `status ${kind}`;
}

// ------------------------------------------------------------------------------ flag list
async function loadFlags(): Promise<void> {
  state.rows = (await api<Row[]>("GET", "/api/flags")).data;
  renderList();
}

function renderList(): void {
  const list = $("#flag-list");
  list.replaceChildren();
  for (const row of state.rows) {
    const cfg = row.flag.environments[state.env];
    const toggle = h("input", { type: "checkbox", "data-testid": `toggle-${row.flag.key}`, title: `on in ${state.env}` }) as HTMLInputElement;
    toggle.checked = cfg.on;
    toggle.addEventListener("click", (e) => e.stopPropagation());
    toggle.addEventListener("change", () => void toggleFlag(row, toggle.checked));
    const item = h("li", { class: row.flag.key === state.selected ? "selected" : "", "data-testid": `flag-${row.flag.key}` },
      toggle, h("span", { class: "key" }, row.flag.key), h("span", { class: "badge" }, `v${row.version}`));
    item.addEventListener("click", () => void select(row.flag.key));
    list.append(item);
  }
}

async function toggleFlag(row: Row, on: boolean): Promise<void> {
  try {
    await api("POST", `/api/flags/${row.flag.key}/toggle`, { env: state.env, on }, { "If-Match": `"${row.version}"` });
    setStatus(`${row.flag.key} turned ${on ? "on" : "off"} in ${state.env}`, "ok");
  } catch (e) {
    setStatus(e instanceof HttpError && e.status === 412
      ? `${row.flag.key} changed since you loaded it; list refreshed, try again` : String((e as Error).message), "error");
  }
  await loadFlags();
  if (state.selected === row.flag.key) await select(row.flag.key, false);
}

// ------------------------------------------------------------------------------ editor
async function select(key: string, resetStatus = true): Promise<void> {
  state.selected = key;
  const { data } = await api<Row>("GET", `/api/flags/${key}`);
  state.loadedVersion = data.version;
  ($("#editor-json") as HTMLTextAreaElement).value = JSON.stringify(data.flag, null, 2);
  $("#editor-title").textContent = `${key} · v${data.version}`;
  $("#conflict").hidden = true;
  if (resetStatus) setStatus("");
  $("#preview").replaceChildren();
  renderList();
  if (state.tab === "audit") await renderAudit();
}

function newFlag(): void {
  state.selected = null;
  state.loadedVersion = 0;
  const template = {
    key: "my-new-flag",
    description: "",
    variations: [false, true],
    environments: { dev: { on: false, off_variation: 0, fallthrough: { variation: 1 } }, prod: { on: false, off_variation: 0, fallthrough: { variation: 0 } } },
  };
  ($("#editor-json") as HTMLTextAreaElement).value = JSON.stringify(template, null, 2);
  $("#editor-title").textContent = "new flag";
  $("#conflict").hidden = true;
  showTab("editor");
  renderList();
}

function readDraft(): Flag | null {
  try {
    return JSON.parse(($("#editor-json") as HTMLTextAreaElement).value) as Flag;
  } catch (e) {
    setStatus(`Invalid JSON: ${(e as Error).message}`, "error");
    return null;
  }
}

async function save(version = state.loadedVersion): Promise<void> {
  const draft = readDraft();
  if (!draft) return;
  const comment = ($("#comment") as HTMLInputElement).value;
  try {
    if (state.selected === null) {
      const { data } = await api<Row>("POST", "/api/flags", draft, { "X-Comment": comment });
      setStatus(`Created ${data.flag.key}`, "ok");
      await loadFlags();
      await select(data.flag.key, false);
      return;
    }
    const { data } = await api<Row & { unchanged?: boolean }>("PUT", `/api/flags/${state.selected}`, draft,
      { "If-Match": `"${version}"`, "X-Comment": comment });
    setStatus(data.unchanged ? "No changes to save" : `Saved v${data.version}`, "ok");
    ($("#comment") as HTMLInputElement).value = "";
    await loadFlags();
    await select(data.flag.key, false);
  } catch (e) {
    if (e instanceof HttpError && e.status === 412) {
      $("#conflict-text").textContent =
        `Someone saved v${e.body.current_version} after you loaded v${state.loadedVersion}. Your draft is untouched.`;
      $("#conflict").dataset.current = String(e.body.current_version);
      $("#conflict").hidden = false;
      setStatus("Save rejected: version conflict", "error");
    } else {
      setStatus((e as Error).message, "error");
    }
  }
}

async function remove(): Promise<void> {
  if (state.selected === null || !confirm(`Delete ${state.selected}?`)) return;
  try {
    await api("DELETE", `/api/flags/${state.selected}`, undefined, { "If-Match": `"${state.loadedVersion}"` });
    setStatus(`Deleted ${state.selected}`, "ok");
    state.selected = null;
    await loadFlags();
    newFlag();
  } catch (e) {
    const deps = e instanceof HttpError && e.body?.dependents ? ` (${e.body.dependents.join(", ")})` : "";
    setStatus((e as Error).message + deps, "error");
  }
}

async function preview(): Promise<void> {
  const draft = readDraft();
  if (!draft || state.selected === null) return;
  const out = $("#preview");
  try {
    const { data } = await api<Preview>("POST", `/api/flags/${state.selected}/preview`, { env: state.env, draft, sample_size: 5000 });
    const pct = ((100 * data.changed) / data.total).toFixed(1);
    const counts = (m: Record<string, number>) => Object.entries(m).map(([v, n]) => `${v}: ${n}`).join(", ");
    out.replaceChildren(
      h("p", { "data-testid": "preview-summary" }, `${data.changed} of ${data.total} synthetic ${state.env} users (${pct}%) would change variation.`),
      h("p", {}, `before → ${counts(data.before)}`), h("p", {}, `after → ${counts(data.after)}`),
      ...data.examples.slice(0, 5).map((x) => h("p", { class: "mono" },
        `${x.context.key}: ${JSON.stringify(x.before.value)} (${x.before.reason.kind}) → ${JSON.stringify(x.after.value)} (${x.after.reason.kind})`)),
    );
  } catch (e) {
    out.replaceChildren(h("p", { class: "error" }, (e as Error).message));
  }
}

// ------------------------------------------------------------------------------ audit
async function renderAudit(): Promise<void> {
  const el = $("#audit");
  if (state.selected === null) { el.replaceChildren(h("p", {}, "Select a flag.")); return; }
  const { data } = await api<AuditEntry[]>("GET", `/api/flags/${state.selected}/audit`);
  el.replaceChildren(...data.map((a) => h("div", { class: "audit-entry", "data-testid": "audit-entry" },
    h("div", { class: "audit-head" }, `v${a.version} · ${a.action} · ${a.actor} · ${new Date(a.at).toLocaleString()}${a.comment ? ` · “${a.comment}”` : ""}`),
    h("ul", {}, ...a.diff.slice(0, 50).map((d) => h("li", { class: "mono" },
      `${d.path}: ${d.op === "added" ? "" : JSON.stringify(d.before) + " → "}${d.op === "removed" ? "(removed)" : JSON.stringify(d.after)}`))),
  )));
}

// ------------------------------------------------------------------------------ playground
async function runPlayground(): Promise<void> {
  const out = $("#playground-out");
  let context: Context;
  try {
    context = JSON.parse(($("#context-json") as HTMLTextAreaElement).value);
  } catch (e) {
    out.replaceChildren(h("p", { class: "error" }, `Invalid JSON: ${(e as Error).message}`));
    return;
  }
  try {
    const server = (await api<Record<string, Result>>("POST", "/api/evaluate", { env: state.env, context })).data;
    const client = new FlagClient("", state.env);
    await client.refresh();
    const rows = Object.keys(server).map((key) => {
      const s = server[key], c = client.detail(key, context);
      const same = jsonEqual(s as unknown as Json, c as unknown as Json); // structural: key order may differ
      const reason = s.reason.kind + (s.reason.rule_id ? ` (${s.reason.rule_id})` : s.reason.prerequisite ? ` (${s.reason.prerequisite})` : "");
      return h("tr", { "data-testid": `eval-${key}` }, h("td", {}, key), h("td", { class: "mono" }, JSON.stringify(s.value)),
        h("td", {}, reason), h("td", { class: same ? "ok" : "error" }, same ? "match" : `MISMATCH: ${JSON.stringify(c.value)}`));
    });
    out.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "flag"), h("th", {}, "value"),
      h("th", {}, "reason"), h("th", {}, "local SDK"))), h("tbody", {}, ...rows)));
  } catch (e) {
    out.replaceChildren(h("p", { class: "error" }, (e as Error).message));
  }
}

// ------------------------------------------------------------------------------ wiring
function showTab(tab: typeof state.tab): void {
  state.tab = tab;
  for (const t of ["editor", "audit", "playground"]) {
    $(`#tab-${t}`).hidden = t !== tab;
    $(`[data-tab="${t}"]`).classList.toggle("active", t === tab);
  }
  if (tab === "audit") void renderAudit();
}

function init(): void {
  ($("#env") as HTMLSelectElement).value = state.env;
  $("#env").addEventListener("change", (e) => { state.env = (e.target as HTMLSelectElement).value; renderList(); });
  ($("#actor") as HTMLInputElement).value = state.actor;
  $("#actor").addEventListener("change", (e) => {
    state.actor = (e.target as HTMLInputElement).value || "me";
    try { localStorage.setItem("flagdeck.actor", state.actor); } catch { /* storage unavailable */ }
  });
  document.querySelectorAll<HTMLElement>("[data-tab]").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab as typeof state.tab)));
  $("#new-flag").addEventListener("click", newFlag);
  $("#save").addEventListener("click", () => void save());
  $("#preview-btn").addEventListener("click", () => void preview());
  $("#delete").addEventListener("click", () => void remove());
  $("#reload-latest").addEventListener("click", () => { if (state.selected) void select(state.selected); });
  $("#overwrite").addEventListener("click", () => void save(Number($("#conflict").dataset.current)));
  $("#run-eval").addEventListener("click", () => void runPlayground());
  void loadFlags().then(() => (state.rows.length ? select(state.rows[0].flag.key) : newFlag()));
}

init();
