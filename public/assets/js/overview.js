// Customer list (left column). Clicking a row (or Enter/Space on it) opens the customer in the panel.
// Times are shown in the customer's zone (the one the calling hours use).

import { customerLabel, inZone, outcomeLabel, statusOf, whenFormat } from "./format.js";
import { onSelect, selectCustomer, selectedCustomerId } from "./selection.js";
import { onLists } from "./store.js";
import { toast } from "./ui.js";

const rows = document.getElementById("overview-rows");
const search = document.getElementById("overview-search");
const filter = document.getElementById("overview-filter");
let customers = [];
let sortKey = "next";
let sortDir = 1;  // 1 ascending, -1 descending

const byName = (a, b) => (a.c.first_name + a.c.last_name).localeCompare(b.c.first_name + b.c.last_name);
// Empty times always sort last, whatever the direction.
const byTime = (field) => (a, b, dir) => {
  const x = a.c[field] || "", y = b.c[field] || "";
  return x === y ? 0 : !x ? 1 : !y ? -1 : dir * x.localeCompare(y);
};
const sorters = {
  name: (a, b, dir) => dir * byName(a, b),
  status: (a, b, dir) => dir * (a.status.order - b.status.order),
  next: byTime("next_call_at"),
  last: byTime("last_call_at"),
};

function cell(row, text, sub, title) {
  const td = row.insertCell();
  td.append(text);
  if (sub) {
    const span = document.createElement("span");
    span.className = "sub";
    span.textContent = sub;
    td.append(span);
  }
  if (title) td.title = title;
  return td;
}

function message(text) {
  rows.replaceChildren();
  const td = rows.insertRow().insertCell();
  td.colSpan = 4;
  td.textContent = text;
}

function render() {
  const terms = search.value.trim().toLowerCase();
  const shown = customers
    .map((c) => ({ c, status: statusOf(c) }))
    .filter(({ c, status }) => (filter.value === "all" || status.key === filter.value || (filter.value === "review" && c.needs_review)) &&
      (!terms || (c.first_name + " " + c.last_name + " " + c.phone).toLowerCase().includes(terms)))
    .sort((a, b) => sorters[sortKey](a, b, sortDir) || byName(a, b));

  if (!customers.length) return message("No customers yet. Add one with “+ New customer”.");
  if (!shown.length) return message("No customers match.");
  const focusedId = document.activeElement?.closest?.("#overview-rows tr[data-id]")?.dataset.id;
  rows.replaceChildren();
  for (const { c, status } of shown) {
    const row = rows.insertRow();
    row.dataset.id = c.id;
    row.tabIndex = 0;
    row.setAttribute("aria-selected", String(c.id === selectedCustomerId()));
    row.addEventListener("click", () => selectCustomer(c.id));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectCustomer(c.id);
      }
    });

    cell(row, customerLabel(c), c.timezone + " · now " + inZone(null, c.timezone, { hour: "2-digit", minute: "2-digit" }));

    const badges = document.createElement("span");
    badges.className = "badges";
    badges.append(Object.assign(document.createElement("span"), { className: "badge " + status.key, textContent: status.label }));
    if (c.needs_review) {
      badges.append(Object.assign(document.createElement("span"), { className: "badge review", textContent: "Review" }));
    }
    cell(row, badges);

    const more = c.pending_callbacks - 1;
    cell(row, c.next_call_at ? inZone(c.next_call_at, c.timezone, whenFormat) : "–",
      c.next_call_at && more > 0 ? "+" + more + " more" : "");

    cell(row, c.last_call_at ? inZone(c.last_call_at, c.timezone, whenFormat) : "Never called",
      c.calling_now ? "In progress" : outcomeLabel(c.last_call_ended_reason),
      c.last_call_ended_reason || "");
  }
  // Rows are rebuilt on every refresh: keep keyboard focus on the same customer.
  if (focusedId) rows.querySelector(`tr[data-id="${CSS.escape(focusedId)}"]`)?.focus();
}

onLists((event) => {
  if (event.list !== "customers") return;
  if (event.state === "loading") message("Loading…");
  else if (event.state === "signedOut") { customers = []; message("Sign in to load customers."); }
  else if (event.state === "error") {
    if (customers.length) toast(event.error, "err");  // keep showing the last good list
    else message(event.error);
  }
  else { customers = event.data; render(); }
});

onSelect((id) => {
  for (const row of rows.querySelectorAll("tr[data-id]")) row.setAttribute("aria-selected", String(row.dataset.id === id));
});

// Clicking a column title sorts by it; clicking it again flips the direction.
for (const button of document.querySelectorAll(".overview th .sort")) {
  button.addEventListener("click", () => {
    sortDir = button.dataset.sort === sortKey ? -sortDir : 1;
    sortKey = button.dataset.sort;
    for (const th of document.querySelectorAll(".overview th")) th.removeAttribute("aria-sort");
    button.parentElement.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
    render();
  });
}

search.addEventListener("input", render);
filter.addEventListener("change", render);
