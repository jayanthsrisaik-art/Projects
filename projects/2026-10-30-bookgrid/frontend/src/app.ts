import { ApiError, Client, describeProblems, type Occurrence, type Resource } from "./api.js";
import { DAY_CODES, buildRRule, describeRRule, type DayCode, type RepeatForm } from "./rrule.js";
import { addDays, hhmmToMinutes, mondayOf, place, snapToGrid, weekDates } from "./grid.js";

const api = new Client();
const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const PX_PER_HOUR = 48;
const STEP = 15;

const state = {
  rooms: [] as Resource[],
  room: null as Resource | null,
  monday: mondayOf(new Date().toISOString().slice(0, 10)),
  selected: null as Occurrence | null,
  // Reused while the form is unchanged so a retried submit can't double-book.
  idempotencyKey: crypto.randomUUID(),
};

const form = $<HTMLFormElement>("#booking-form");
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;

function el(tag: string, attrs: Record<string, string> = {}, text?: string): HTMLElement {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function showResult(target: HTMLElement, kind: "ok" | "err", title: string, lines: string[] = []) {
  target.replaceChildren();
  const box = el("div", { class: kind }, title);
  if (lines.length) {
    const ul = el("ul");
    for (const line of lines) ul.append(el("li", {}, line));
    box.append(ul);
  }
  target.append(box);
}

function showError(target: HTMLElement, err: unknown) {
  if (err instanceof ApiError) showResult(target, "err", err.message, err.lines());
  else showResult(target, "err", err instanceof Error ? err.message : String(err));
}

async function loadRooms() {
  state.rooms = await api.resources();
  const select = $<HTMLSelectElement>("#room");
  select.replaceChildren(...state.rooms.map((r) => el("option", { value: String(r.id) }, r.name)));
  state.room = state.rooms[0] ?? null;
}

async function render() {
  const room = state.room;
  if (!room) return;
  $("#room-meta").textContent =
    `${room.timezone} · ${room.open_time}-${room.close_time} · ${room.buffer_minutes} min turnaround`;
  $<HTMLInputElement>("#week").value = state.monday;
  const days = weekDates(state.monday);
  const occ = await api.occurrences(room.id, days[0], days[6]);
  const dayStart = Math.floor(hhmmToMinutes(room.open_time) / 60) * 60;
  const dayEnd = Math.ceil(hhmmToMinutes(room.close_time) / 60) * 60;
  const height = ((dayEnd - dayStart) / 60) * PX_PER_HOUR;

  const grid = $("#grid");
  grid.replaceChildren(el("div"));
  days.forEach((d, i) => {
    const closed = !room.open_days.includes(DAY_CODES[i]);
    const label = new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, {
      weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
    grid.append(el("div", { class: `colhead${closed ? " closed" : ""}` }, label));
  });
  const hours = el("div", { class: "hours", style: `height:${height}px` });
  for (let m = dayStart + 60; m < dayEnd; m += 60) {
    const tick = el("span", { style: `top:${((m - dayStart) / 60) * PX_PER_HOUR}px` },
      `${String(m / 60).padStart(2, "0")}:00`);
    hours.append(tick);
  }
  grid.append(hours);
  const columns = days.map((d, i) => {
    const closed = !room.open_days.includes(DAY_CODES[i]);
    const col = el("div", { class: `day${closed ? " closed" : ""}`,
      style: `height:${height}px; --hour:${PX_PER_HOUR}px`, "data-date": d });
    if (!closed) {
      col.addEventListener("click", (ev) => {
        if (ev.target !== col) return;
        const fraction = (ev as MouseEvent).offsetY / col.clientHeight;
        field("date").value = d;
        field("start_time").value = snapToGrid(fraction, dayStart, dayEnd, STEP);
        void refreshFree();
        field("title").focus();
      });
    }
    grid.append(col);
    return col;
  });
  for (const o of occ) {
    const p = place(o.local_start, o.local_end, state.monday, dayStart, dayEnd);
    if (!p) continue;
    const block = el("div", {
      class: `block${state.selected?.id === o.id ? " selected" : ""}`,
      style: `top:${p.topPct}%; height:${p.heightPct}%`,
      title: `${o.title} — ${o.organizer}${o.note ? `\n${o.note}` : ""}`,
    });
    block.append(document.createTextNode(o.title));
    block.append(el("small", {}, `${o.local_start.slice(11, 16)}-${o.local_end.slice(11, 16)}${o.rrule ? " ↻" : ""}`));
    block.addEventListener("click", () => selectOccurrence(o));
    columns[p.dayIndex].append(block);
  }
}

function selectOccurrence(o: Occurrence) {
  state.selected = o;
  const details = $("#details");
  details.hidden = false;
  $("#d-title").textContent = o.title;
  $("#d-meta").textContent = `${o.local_start.slice(0, 10)} ${o.local_start.slice(11, 16)}-${o.local_end.slice(11, 16)}`
    + ` · ${o.organizer} · ${describeRRule(o.rrule)}${o.moved ? " · moved" : ""}${o.note ? ` · ${o.note}` : ""}`;
  $<HTMLInputElement>("#d-date").value = o.local_start.slice(0, 10);
  $<HTMLInputElement>("#d-time").value = o.local_start.slice(11, 16);
  $("#d-result").replaceChildren();
  for (const b of details.querySelectorAll<HTMLButtonElement>("button[data-scope]")) {
    b.hidden = !o.rrule && b.dataset.scope !== "this";
  }
  void render();
}

function repeatForm(): RepeatForm {
  const days = [...form.querySelectorAll<HTMLInputElement>("input[name=day]:checked")].map((c) => c.value as DayCode);
  const endKind = (form.querySelector("input[name=end_kind]:checked") as HTMLInputElement).value;
  return {
    freq: field("freq").value as RepeatForm["freq"],
    interval: Number(field("interval").value),
    days,
    end: endKind === "count" ? { kind: "count", count: Number(field("count").value) }
      : { kind: "until", until: field("until").value },
  };
}

function bookingInput() {
  if (!state.room) throw new Error("No room selected");
  return {
    resource_id: state.room.id,
    title: field("title").value.trim(),
    organizer: field("organizer").value.trim(),
    date: field("date").value,
    start_time: field("start_time").value,
    duration_minutes: Number(field("duration").value),
    rrule: buildRRule(repeatForm()),
    on_conflict: field("on_conflict").value as "reject" | "skip",
  };
}

async function refreshFree() {
  const date = field("date").value;
  const room = state.room;
  const target = $("#free-slots");
  $("#free-label").textContent = date || "…";
  if (!room || !date) return target.replaceChildren();
  const res = await api.free(room.id, date, Number(field("duration").value) || 30, 30);
  if (res.closed) return target.replaceChildren(el("span", { class: "muted" }, "Closed that day"));
  if (!res.slots.length) return target.replaceChildren(el("span", { class: "muted" }, "Nothing free"));
  target.replaceChildren(...res.slots.map((s) => {
    const b = el("button", { type: "button" }, s.local_start.slice(11, 16));
    b.addEventListener("click", () => { field("start_time").value = s.local_start.slice(11, 16); });
    return b;
  }));
}

function updateRepeatUi() {
  const freq = field("freq").value;
  $("#repeat-options").hidden = freq === "none";
  $("#interval-unit").textContent = freq === "DAILY" ? "days" : "weeks";
  $("#days").hidden = freq !== "WEEKLY";
  try {
    $("#rule-text").textContent = describeRRule(buildRRule(repeatForm()));
  } catch (e) {
    $("#rule-text").textContent = (e as Error).message;
  }
}

function wire() {
  $("#days").replaceChildren(...DAY_CODES.map((d) => {
    const label = el("label");
    label.append(el("input", { type: "checkbox", name: "day", value: d }), document.createTextNode(d));
    return label;
  }));
  form.addEventListener("input", () => {
    state.idempotencyKey = crypto.randomUUID();
    updateRepeatUi();
  });
  field("date").addEventListener("change", () => void refreshFree());
  field("duration").addEventListener("change", () => void refreshFree());
  $("#check").addEventListener("click", async () => {
    const out = $("#form-result");
    try {
      const pv = await api.preview(bookingInput());
      const lines = pv.occurrences.map((o) =>
        `${o.local_start.slice(0, 10)} ${o.local_start.slice(11, 16)} — ${o.problems ? describeProblems(o.problems) : "free"}${o.note ? ` (${o.note})` : ""}`);
      showResult(out, pv.blocked ? "err" : "ok", `${pv.bookable} bookable, ${pv.blocked} blocked`, lines);
    } catch (e) { showError(out, e); }
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const out = $("#form-result");
    try {
      const res = await api.book(bookingInput(), state.idempotencyKey);
      showResult(out, "ok", `Booked ${res.booked.length} occurrence(s)`,
        res.skipped.map((s) => `skipped ${s.recurrence_date}: ${describeProblems(s.problems ?? [])}`));
      state.idempotencyKey = crypto.randomUUID();
      state.monday = mondayOf(field("date").value);
      await render();
      await refreshFree();
    } catch (e) { showError(out, e); }
  });
  $("#room").addEventListener("change", async (ev) => {
    state.room = state.rooms.find((r) => String(r.id) === (ev.target as HTMLSelectElement).value) ?? null;
    state.selected = null;
    $("#details").hidden = true;
    $("#form-result").replaceChildren();
    await render();
    await refreshFree();
  });
  $("#prev").addEventListener("click", () => { state.monday = addDays(state.monday, -7); void render(); });
  $("#next").addEventListener("click", () => { state.monday = addDays(state.monday, 7); void render(); });
  $("#week").addEventListener("change", (ev) => {
    const v = (ev.target as HTMLInputElement).value;
    if (v) { state.monday = mondayOf(v); void render(); }
  });
  $("#d-move").addEventListener("click", async () => {
    const o = state.selected;
    if (!o) return;
    try {
      state.selected = await api.move(o.id, $<HTMLInputElement>("#d-date").value, $<HTMLInputElement>("#d-time").value);
      selectOccurrence(state.selected);
      showResult($("#d-result"), "ok", "Moved");
    } catch (e) { showError($("#d-result"), e); }
  });
  for (const b of document.querySelectorAll<HTMLButtonElement>("#details button[data-scope]")) {
    b.addEventListener("click", async () => {
      const o = state.selected;
      if (!o) return;
      try {
        const r = await api.cancel(o.id, b.dataset.scope as "this" | "following" | "series");
        state.selected = null;
        await render();
        showResult($("#d-result"), "ok", `Cancelled ${r.cancelled} occurrence(s)`);
        await refreshFree();
      } catch (e) { showError($("#d-result"), e); }
    });
  }
}

async function main() {
  wire();
  const params = new URLSearchParams(location.search);
  const week = params.get("week");
  if (week) state.monday = mondayOf(week);
  field("date").value = state.monday;
  updateRepeatUi();
  await loadRooms();
  await render();
  await refreshFree();
}

void main();
