// tallyup single-page UI. No framework: a tiny element helper and explicit re-renders.
import {
  ApiError, Client, newIdempotencyKey,
  type Expense, type ExpenseIn, type Group, type Split,
} from "./api.js";
import { MoneyError, parseAmount } from "./money.js";

const api = new Client();
const root = document.getElementById("app")!;

type Attrs = Record<string, string | number | boolean | ((ev: Event) => void) | undefined>;
function h(tag: string, attrs: Attrs = {}, ...children: (Node | string | null | false)[]): HTMLElement {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (k === "value") (el as HTMLInputElement).value = String(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) if (c !== null && c !== false) el.append(c);
  return el;
}

function errorText(e: unknown): string {
  if (e instanceof ApiError || e instanceof MoneyError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

// ------------------------------------------------------------------ landing
async function renderLanding(): Promise<void> {
  const currencies = (await fetch("/api/currencies").then((r) => r.json())) as Record<string, number>;
  const msg = h("p", { class: "error", role: "alert" });
  const name = h("input", { id: "group-name", placeholder: "Lisbon trip", required: true }) as HTMLInputElement;
  const cur = h("select", { id: "group-currency" },
    ...Object.keys(currencies).map((c) => h("option", { value: c }, c))) as HTMLSelectElement;
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        const g = await api.createGroup(name.value, cur.value);
        location.hash = `#/g/${g.id}`;
      } catch (e) { msg.textContent = errorText(e); }
    },
  },
  h("label", { for: "group-name" }, "Group name"), name,
  h("label", { for: "group-currency" }, "Currency"), cur,
  h("button", { type: "submit" }, "Create group"), msg);
  root.replaceChildren(h("section", { class: "card" }, h("h2", {}, "Start a group"), form));
}

// -------------------------------------------------------------------- group
interface State {
  group: Group;
  expenses: Expense[];
  editing: Expense | null;
  draftKey: string;            // idempotency key for the expense currently being drafted
  method: "optimal" | "greedy";
}

async function renderGroup(gid: number): Promise<void> {
  const state: State = {
    group: await api.group(gid), expenses: [], editing: null,
    draftKey: newIdempotencyKey(), method: "optimal",
  };
  const membersBox = h("section", { class: "card" });
  const formBox = h("section", { class: "card" });
  const listBox = h("section", { class: "card" });
  const balBox = h("section", { class: "card" });
  root.replaceChildren(
    h("h2", {}, `${state.group.name} `, h("small", {}, `(${state.group.currency}, group #${gid})`)),
    h("div", { class: "grid" }, h("div", {}, membersBox, formBox), h("div", {}, balBox, listBox)),
  );
  const name = (id: number) => state.group.members.find((m) => m.id === id)?.name ?? `#${id}`;

  async function refresh(): Promise<void> {
    state.group = await api.group(gid);
    state.expenses = await api.expenses(gid);
    renderMembers(); renderForm(); renderList(); await renderBalances();
  }

  function renderMembers(): void {
    const input = h("input", { id: "member-name", placeholder: "Name" }) as HTMLInputElement;
    const msg = h("p", { class: "error", role: "alert" });
    membersBox.replaceChildren(
      h("h3", {}, "Members"),
      h("ul", { class: "chips" }, ...state.group.members.map((m) => h("li", {}, m.name))),
      h("form", {
        class: "inline",
        onsubmit: async (ev) => {
          ev.preventDefault();
          try { await api.addMember(gid, input.value); await refresh(); } catch (e) { msg.textContent = errorText(e); }
        },
      }, input, h("button", { type: "submit" }, "Add")),
      msg);
  }

  function renderForm(): void {
    const g = state.group;
    if (g.members.length === 0) {
      formBox.replaceChildren(h("h3", {}, "Add expense"), h("p", {}, "Add members first."));
      return;
    }
    const e = state.editing;
    const desc = h("input", { id: "exp-desc", placeholder: "Dinner", value: e?.description ?? "" }) as HTMLInputElement;
    const amount = h("input", { id: "exp-amount", inputmode: "decimal", placeholder: "0.00", value: e?.amount ?? "" }) as HTMLInputElement;
    const payer = h("select", { id: "exp-payer" },
      ...g.members.map((m) => h("option", { value: m.id, selected: e?.payer_id === m.id }, m.name))) as HTMLSelectElement;
    const kind = h("select", { id: "exp-kind" },
      ...(["equal", "shares", "percent", "exact"] as const).map((k) =>
        h("option", { value: k, selected: (e?.split.kind ?? "equal") === k }, k))) as HTMLSelectElement;
    const preview = h("p", { class: "muted", "aria-live": "polite" });
    const msg = h("p", { class: "error", role: "alert" });
    const rows = h("div", { class: "split-rows" });
    const prior = new Map((e?.split.parts ?? []).map((p) => [p.member_id, p.value]));

    const inputs = new Map<number, { on: HTMLInputElement; val: HTMLInputElement }>();
    function drawRows(): void {
      rows.replaceChildren();
      inputs.clear();
      for (const m of g.members) {
        const on = h("input", { type: "checkbox", checked: e ? prior.has(m.id) : true, oninput: schedule }) as HTMLInputElement;
        const val = h("input", {
          class: "val", placeholder: kind.value === "percent" ? "%" : kind.value === "shares" ? "1" : "0.00",
          value: e && e.split.kind === kind.value ? String(prior.get(m.id) ?? "") : "", oninput: schedule,
          hidden: kind.value === "equal",
        }) as HTMLInputElement;
        inputs.set(m.id, { on, val });
        rows.append(h("label", { class: "split-row" }, on, h("span", {}, m.name), val));
      }
    }

    function buildBody(): ExpenseIn {
      parseAmount(amount.value, g.exponent); // fail fast with the same rules as the server
      const parts: Split["parts"] = [];
      for (const [mid, { on, val }] of inputs) {
        if (!on.checked) continue;
        parts.push(kind.value === "equal" ? { member_id: mid } : { member_id: mid, value: val.value.trim() });
      }
      return { description: desc.value, payer_id: Number(payer.value), amount: amount.value.trim(),
               split: { kind: kind.value as Split["kind"], parts } };
    }

    let timer: number | undefined;
    function schedule(): void {
      window.clearTimeout(timer);
      timer = window.setTimeout(async () => {
        try {
          const body = buildBody();
          const p = await api.preview(gid, { ...body, description: body.description || "preview",
                                             expense_id: e?.id });
          preview.textContent = "Shares: " + p.shares.map((s) => `${name(s.member_id)} ${s.amount}`).join(", ");
          msg.textContent = "";
        } catch (err) {
          preview.textContent = "";
          if (amount.value) msg.textContent = errorText(err);
        }
      }, 250);
    }
    kind.addEventListener("change", () => { drawRows(); schedule(); });
    for (const el of [amount, payer, desc]) el.addEventListener("input", schedule);
    drawRows();

    const submit = h("button", { type: "submit" }, e ? `Save changes (v${e.version})` : "Add expense") as HTMLButtonElement;
    const form = h("form", {
      onsubmit: async (ev) => {
        ev.preventDefault();
        submit.disabled = true;
        try {
          const body = buildBody();
          if (e) {
            await api.updateExpense(e.id, body, e.version);
            state.editing = null;
          } else {
            await api.createExpense(gid, body, state.draftKey);
            state.draftKey = newIdempotencyKey(); // next draft is a new expense
          }
          await refresh();
        } catch (err) {
          if (err instanceof ApiError && (err.status === 412 || (e && err.status === 404))) {
            // Lost an edit race: load the winner's version into the form instead of overwriting it.
            state.expenses = await api.expenses(gid);
            state.editing = state.expenses.find((x) => x.id === e?.id) ?? null;
            renderList(); renderForm(); await renderBalances();
            formBox.querySelector(".error")!.textContent = state.editing
              ? "Someone else changed this expense. Their version is loaded; review and save again."
              : "Someone else deleted this expense.";
            return;
          }
          msg.textContent = errorText(err);
        } finally { submit.disabled = false; }
      },
    },
    h("label", { for: "exp-desc" }, "Description"), desc,
    h("div", { class: "row" },
      h("div", {}, h("label", { for: "exp-amount" }, `Amount (${g.currency})`), amount),
      h("div", {}, h("label", { for: "exp-payer" }, "Paid by"), payer),
      h("div", {}, h("label", { for: "exp-kind" }, "Split"), kind)),
    rows, preview, msg,
    h("div", { class: "row" }, submit,
      e ? h("button", { type: "button", class: "ghost", onclick: () => { state.editing = null; renderForm(); } }, "Cancel") : null));
    formBox.replaceChildren(h("h3", {}, e ? `Edit expense #${e.id}` : "Add expense"), form);
  }

  function renderList(): void {
    listBox.replaceChildren(h("h3", {}, "Expenses"),
      state.expenses.length === 0 ? h("p", { class: "muted" }, "No expenses yet.") :
        h("ul", { class: "expenses" }, ...state.expenses.map((x) => h("li", {},
          h("div", {}, h("strong", {}, x.description), ` ${x.amount} paid by ${name(x.payer_id)}`,
            h("div", { class: "muted" }, `${x.split.kind}: ` +
              x.shares.map((s) => `${name(s.member_id)} ${s.amount}`).join(", "))),
          h("div", { class: "actions" },
            h("button", { class: "ghost", onclick: () => { state.editing = x; renderForm(); } }, "Edit"),
            h("button", {
              class: "ghost danger",
              onclick: async () => {
                try { await api.deleteExpense(x.id, x.version); }
                catch (err) { alert(errorText(err)); }
                await refresh();
              },
            }, "Delete"))))));
  }

  async function renderBalances(): Promise<void> {
    const [b, plan] = await Promise.all([api.balances(gid), api.settle(gid, state.method)]);
    const toggle = h("select", {
      id: "settle-method",
      onchange: async (ev) => { state.method = (ev.target as HTMLSelectElement).value as State["method"]; await renderBalances(); },
    }, h("option", { value: "optimal", selected: state.method === "optimal" }, "fewest transfers"),
       h("option", { value: "greedy", selected: state.method === "greedy" }, "greedy"));
    balBox.replaceChildren(
      h("h3", {}, "Balances"),
      h("table", {}, h("tbody", {}, ...b.balances.map((r) => h("tr", {},
        h("td", {}, r.name),
        h("td", { class: r.balance_minor < 0 ? "neg num" : "pos num" }, r.balance),
        h("td", { class: "muted num", title: "cumulative rounding drift in minor units" },
          r.rounding_drift_minor === 0 ? "" : `drift ${r.rounding_drift_minor.toFixed(2)}`))))),
      h("div", { class: "row" }, h("h3", {}, "Settle up"), toggle),
      h("p", { class: "muted" }, `${plan.transfer_count} transfer(s) (${plan.method}); greedy needs ${plan.greedy_transfer_count}.`),
      plan.transfers.length === 0 ? h("p", {}, "Everyone is settled.") :
        h("ul", { class: "transfers" }, ...plan.transfers.map((t) => {
          const key = newIdempotencyKey();
          const btn = h("button", {
            class: "ghost",
            onclick: async () => {
              (btn as HTMLButtonElement).disabled = true;
              try { await api.pay(gid, { from_member: t.from_member, to_member: t.to_member, amount: t.amount }, key); }
              catch (err) { alert(errorText(err)); }
              await refresh();
            },
          }, "Mark paid");
          return h("li", {}, `${t.from_name} pays ${t.to_name} ${t.amount} `, btn);
        })));
  }

  await refresh();
}

async function route(): Promise<void> {
  const m = /^#\/g\/(\d+)$/.exec(location.hash);
  try {
    if (m) await renderGroup(Number(m[1]));
    else await renderLanding();
  } catch (e) {
    root.replaceChildren(h("p", { class: "error" }, errorText(e)), h("a", { href: "#/" }, "Back"));
  }
}

window.addEventListener("hashchange", route);
void route();
