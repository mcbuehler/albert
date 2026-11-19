// "Schedule callback" dialog for the selected customer (opened from the panel).
// The time is entered in the customer's zone and may be any time; only retries keep calling hours.
// Quick picks fill the time; a warning shows before submitting if the time has passed.

import { api, pageConfig } from "./auth.js";
import { addMinutes, atTime, localNow, naiveLabel } from "./format.js";
import { selectedCustomer } from "./selection.js";
import { loadCustomers, onLists } from "./store.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const dialog = document.getElementById("schedule-dialog");
const form = document.getElementById("schedule-form");
const input = form.elements.local_time;
const status = document.getElementById("schedule-status");
const hoursHint = document.getElementById("schedule-hours");
const warning = document.getElementById("schedule-warning");
const picks = document.getElementById("schedule-picks");

const hours = () => ({ start: pageConfig.calling_start || "09:00", end: pageConfig.calling_end || "22:00" });

// The customer's zone and local time are already in the panel header and the list.
function showHours() {
  hoursHint.textContent = `If nobody answers, retries happen ${hours().start}–${hours().end}.`;
}

function showWarning() {
  const customer = selectedCustomer();
  const value = input.value;
  let text = "";
  if (customer && value && value <= localNow(customer.timezone)) text = "This time has already passed for the customer.";
  warning.textContent = text;
  warning.hidden = !text;
}

// Quick picks, in the customer's local time.
const PICKS = [
  ["In 1 hour", (now) => {
    const later = addMinutes(now, 60);
    return addMinutes(later, (5 - Number(later.slice(14, 16)) % 5) % 5);  // round up to 5 minutes
  }],
  [() => "Tomorrow " + hours().start, (now) => atTime(now, 1, hours().start)],
  ["Tomorrow 14:00", (now) => atTime(now, 1, "14:00")],
];

function renderPicks() {
  picks.replaceChildren(...PICKS.map(([label, pick]) => {
    const button = Object.assign(document.createElement("button"), {
      type: "button", className: "link-btn", textContent: typeof label === "function" ? label() : label,
    });
    button.addEventListener("click", () => {
      const customer = selectedCustomer();
      if (!customer) return;
      input.value = pick(localNow(customer.timezone));
      showWarning();
    });
    return button;
  }));
}

input.addEventListener("input", showWarning);
onLists((event) => { if (event.list === "customers") showWarning(); });
document.getElementById("open-schedule").addEventListener("click", () => {
  const customer = selectedCustomer();
  if (!customer) return;
  form.reset();
  setStatus(status, "", "");
  document.getElementById("schedule-dialog-title").textContent = `Schedule a callback for ${customer.first_name} ${customer.last_name}`;
  renderPicks();
  showHours();
  showWarning();
  dialog.showModal();
  input.focus();
});
dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const customer = selectedCustomer();
  if (!customer) return;
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  setStatus(status, "", "Scheduling…");
  const fields = { ...Object.fromEntries(new FormData(form)), phone: customer.phone };
  try {
    const { res, data } = await postJson(api, "/api/scheduled-calls", fields);
    if (res.ok) {
      dialog.close();
      loadCustomers();
      toast(data.already_scheduled
        ? `${customer.first_name} ${customer.last_name} already has a callback on ${naiveLabel(data.due_local_time)}.`
        : `Callback for ${customer.first_name} ${customer.last_name} on ${naiveLabel(data.due_local_time)}.`);
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
});
