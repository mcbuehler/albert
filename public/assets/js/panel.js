// Detail panel (right column) for the selected customer: contact, status, pending callbacks, last call.
// The schedule and call forms in the panel live in schedule.js and call.js.

import { api } from "./auth.js";
import { inZone, outcomeLabel, statusOf, whenFormat } from "./format.js";
import { onSelect, selectedCustomer } from "./selection.js";
import { loadCustomers, onLists } from "./store.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const empty = document.getElementById("panel-empty");
const body = document.getElementById("panel-body");
const pending = document.getElementById("panel-pending");

function render() {
  const c = selectedCustomer();
  empty.hidden = Boolean(c);
  body.hidden = !c;
  if (!c) return;

  const status = statusOf(c);
  document.getElementById("panel-name").textContent = c.first_name + " " + c.last_name;
  const badge = document.getElementById("panel-badge");
  badge.className = "badge " + status.key;
  badge.textContent = status.label;
  document.getElementById("panel-review-badge").hidden = !c.needs_review;
  document.getElementById("panel-review").hidden = !c.needs_review;
  document.getElementById("panel-contact").textContent =
    c.phone + " · " + c.timezone + " · now " + inZone(null, c.timezone, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  document.getElementById("panel-optout").hidden = !c.opted_out;
  document.getElementById("panel-actions").hidden = c.opted_out;

  const lastCall = document.getElementById("panel-last-call");
  if (!c.last_call_at) lastCall.textContent = "Never called.";
  else {
    lastCall.textContent = inZone(c.last_call_at, c.timezone, whenFormat) + " · " +
      (c.calling_now ? "In progress" : outcomeLabel(c.last_call_ended_reason));
    lastCall.title = c.last_call_ended_reason || "";
  }

  renderPending(c);
}

function renderPending(c) {
  if (!c.pending.length) {
    pending.replaceChildren(Object.assign(document.createElement("li"), { className: "panel-meta", textContent: "None." }));
    return;
  }
  pending.replaceChildren(...c.pending.map((callback) => {
    const item = document.createElement("li");
    const attempt = callback.attempts > 0 ? "retry, attempt " + (callback.attempts + 1) : "first attempt";
    item.append(inZone(callback.due_at, c.timezone, whenFormat) + " · " + attempt);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Cancel";
    const status = document.createElement("span");
    status.className = "status";
    status.setAttribute("role", "status");
    button.addEventListener("click", async () => {
      if (!button.classList.contains("confirm")) {  // first click arms, second click cancels
        button.classList.add("confirm");
        button.textContent = "Confirm cancel";
        return;
      }
      button.disabled = true;
      status.textContent = "Cancelling…";
      try {
        const { res, data } = await postJson(api, "/api/scheduled-calls/" + encodeURIComponent(callback.id) + "/cancel");
        if (res.ok) {
          toast("Callback on " + inZone(callback.due_at, c.timezone, whenFormat) + " cancelled.");
          loadCustomers();
        } else {
          setStatus(status, "err", failedText(res, data));
          button.disabled = false;
        }
      } catch (err) {
        setStatus(status, "err", "Failed: " + err.message);
        button.disabled = false;
      }
    });
    item.append(button, status);
    if (callback.instructions) {
      // Staff instructions for this callback (textContent: shown as plain text, never as HTML).
      item.append(Object.assign(document.createElement("p"), {
        className: "instructions", textContent: "Instructions: " + callback.instructions,
      }));
    }
    return item;
  }));
}

onSelect(render);

// Narrow screens (one column): jump to the panel when a customer is picked, and back to their row.
const oneColumn = window.matchMedia("(max-width: 900px)");
const panel = document.getElementById("customer-panel");

onSelect((id) => {
  if (id && oneColumn.matches) panel.scrollIntoView({ behavior: "smooth", block: "start" });
});

document.getElementById("panel-back").addEventListener("click", () => {
  const c = selectedCustomer();
  const row = c && document.querySelector(`#overview-rows tr[data-id="${CSS.escape(c.id)}"]`);
  const target = row || document.getElementById("customers-title");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  if (row) row.focus({ preventScroll: true });
});
onLists((event) => { if (event.list === "customers") render(); });
