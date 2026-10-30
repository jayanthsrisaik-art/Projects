import { api, ApiError, type Booking, type EventTypeInfo } from "./api.js";
import { dateKey, groupWeek, isValidTimeZone, localDateKey, localParts, localTimeLabel, addDays, weekRangeIso, weekStart } from "./time.js";

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;

const state = {
  types: [] as EventTypeInfo[],
  slug: "",
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  week: "",
  picked: null as string | null,
  idemKey: "",
};

function todayKey(tz: string): string {
  return dateKey(localParts(Date.now(), tz));
}

function setStatus(text: string, kind: "info" | "error" | "ok" = "info") {
  const el = $<HTMLParagraphElement>("#status");
  el.textContent = text;
  el.dataset.kind = kind;
}

function fillTimeZones() {
  const sel = $<HTMLSelectElement>("#tz");
  const common = ["UTC", "America/Los_Angeles", "America/New_York", "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney"];
  const all: string[] = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? common;
  const zones = [...new Set([state.tz, ...common, ...all])].filter(isValidTimeZone);
  sel.innerHTML = zones.map((z) => `<option value="${z}">${z}</option>`).join("");
  sel.value = state.tz;
}

async function loadSlots() {
  const et = state.types.find((t) => t.slug === state.slug);
  if (!et) return;
  const range = weekRangeIso(state.week, state.tz);
  $("#week-label").textContent = `Week of ${state.week} (${state.tz})`;
  $("#host-note").textContent = `${et.title}: ${et.duration_min} min, host is in ${et.host_tz}`;
  const grid = $<HTMLDivElement>("#grid");
  grid.setAttribute("aria-busy", "true");
  try {
    const res = await api.slots(et.slug, range.start, range.end);
    const cols = groupWeek(res.slots, state.tz, state.week);
    grid.innerHTML = "";
    for (const col of cols) {
      const div = document.createElement("div");
      div.className = "day";
      const [y, m, d] = col.date.split("-").map(Number);
      const name = new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
      div.innerHTML = `<h3>${name} <small>${col.date}</small></h3>`;
      if (!col.slots.length) div.insertAdjacentHTML("beforeend", `<p class="none">—</p>`);
      for (const s of col.slots) {
        const b = document.createElement("button");
        b.className = "slot";
        b.textContent = s.label;
        b.dataset.start = s.start;
        b.addEventListener("click", () => pick(s.start));
        div.appendChild(b);
      }
      grid.appendChild(div);
    }
    setStatus(`${res.slots.length} open slot(s) this week`);
  } catch (e) {
    setStatus((e as Error).message, "error");
  } finally {
    grid.removeAttribute("aria-busy");
  }
}

function pick(start: string) {
  state.picked = start;
  // One key per chosen slot: a double submit or a retry after a network error
  // replays the same booking instead of creating a second one.
  state.idemKey = crypto.randomUUID();
  $("#picked").textContent = `${localDateKey(start, state.tz)} ${localTimeLabel(start, state.tz)} ${state.tz}`;
  $<HTMLFormElement>("#book-form").hidden = false;
  $<HTMLInputElement>("#name").focus();
}

function showConfirmation(b: Booking, token: string) {
  const box = $<HTMLDivElement>("#confirmation");
  box.hidden = false;
  box.innerHTML = `
    <h2>Booked</h2>
    <p id="confirm-text">${localDateKey(b.start, state.tz)} ${localTimeLabel(b.start, state.tz)}–${localTimeLabel(b.end, state.tz)} (${state.tz})</p>
    <p><a href="/api/bookings/${b.id}/ics" download>Add to calendar (.ics)</a></p>
    <button id="cancel-btn" type="button">Cancel this booking</button>`;
  $("#cancel-btn").addEventListener("click", async () => {
    try {
      const c = await api.cancel(b.id, token);
      box.querySelector("h2")!.textContent = c.status === "cancelled" ? "Cancelled" : "Booked";
      $("#cancel-btn").remove();
      await loadSlots();
    } catch (e) {
      setStatus((e as Error).message, "error");
    }
  });
}

async function submit(ev: SubmitEvent) {
  ev.preventDefault();
  if (!state.picked) return;
  const name = $<HTMLInputElement>("#name").value;
  const email = $<HTMLInputElement>("#email").value;
  try {
    const res = await api.book(state.slug, state.picked, name, email, state.idemKey);
    $<HTMLFormElement>("#book-form").hidden = true;
    showConfirmation(res.booking, res.token);
    setStatus(res.replayed ? "Booking already confirmed" : "Booking confirmed", "ok");
    state.picked = null;
    await loadSlots();
  } catch (e) {
    if (e instanceof ApiError && e.code === "slot_unavailable") {
      setStatus("Someone just took that slot — pick another.", "error");
      $<HTMLFormElement>("#book-form").hidden = true;
      await loadSlots();
    } else {
      setStatus((e as Error).message, "error");
    }
  }
}

async function init() {
  fillTimeZones();
  state.week = weekStart(todayKey(state.tz));
  state.types = await api.eventTypes();
  const sel = $<HTMLSelectElement>("#event-type");
  sel.innerHTML = state.types.map((t) => `<option value="${t.slug}">${t.title}</option>`).join("");
  state.slug = state.types[0]?.slug ?? "";
  sel.addEventListener("change", () => { state.slug = sel.value; loadSlots(); });
  $<HTMLSelectElement>("#tz").addEventListener("change", (e) => {
    state.tz = (e.target as HTMLSelectElement).value;
    state.week = weekStart(todayKey(state.tz));
    loadSlots();
  });
  $("#prev").addEventListener("click", () => { state.week = addDays(state.week, -7); loadSlots(); });
  $("#next").addEventListener("click", () => { state.week = addDays(state.week, 7); loadSlots(); });
  $<HTMLFormElement>("#book-form").addEventListener("submit", submit);
  const params = new URLSearchParams(location.search);
  if (params.get("week")) state.week = weekStart(params.get("week")!);
  if (params.get("tz") && isValidTimeZone(params.get("tz")!)) {
    state.tz = params.get("tz")!;
    $<HTMLSelectElement>("#tz").value = state.tz;
  }
  if (params.get("type")) { state.slug = params.get("type")!; sel.value = state.slug; }
  await loadSlots();
}

init().catch((e) => setStatus(String(e), "error"));
