// Edit zone in the panel: the selected customer's name, time zone and default assistant.
// Only changed fields are sent; staff edits survive the background refresh unless the saved values change.

import { api } from "./auth.js";
import { onSelect, selectedCustomer } from "./selection.js";
import { loadCustomers, onLists } from "./store.js";
import { createTimezonePicker } from "./timezone-picker.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const zone = document.getElementById("panel-edit");
const form = document.getElementById("edit-form");
const select = form.elements.vapi_assistant_id;
const save = form.querySelector("button[type=submit]");
const status = document.getElementById("edit-status");
const TEXT_FIELDS = ["first_name", "middle_name", "last_name"];
const picker = createTimezonePicker({
  search: document.getElementById("edit-tz-search"),
  list: document.getElementById("edit-tz-list"),
  input: form.elements.timezone,
  onChange: () => { setStatus(status, "", ""); update(); },
});
let shown = null;  // saved values the form was last filled from

function saved(c) {
  return { first_name: c.first_name, middle_name: c.middle_name || "", last_name: c.last_name,
           timezone: c.timezone, vapi_assistant_id: c.vapi_assistant_id };
}

// Fields that differ from the customer's saved values (the assistant only once Vapi's list is in).
function changes(c) {
  const before = saved(c);
  const result = {};
  for (const field of TEXT_FIELDS) {
    const value = form.elements[field].value.trim();
    if (value !== before[field]) result[field] = value;
  }
  if (picker.value() && picker.value() !== before.timezone) result.timezone = picker.value();
  if (!select.disabled && select.value && select.value !== before.vapi_assistant_id) result.vapi_assistant_id = select.value;
  return result;
}

function update() {
  const c = selectedCustomer();
  save.disabled = !c || !Object.keys(changes(c)).length;
}

// Fill the form with the customer's saved values, unless staff are editing the same, unchanged customer.
// (An assistant that no longer exists in Vapi shows as "Choose an assistant".)
function sync(force) {
  const c = selectedCustomer();
  if (!c) { shown = null; update(); return; }
  const key = JSON.stringify({ id: c.id, ...saved(c) });
  if (force || key !== shown) {
    for (const field of TEXT_FIELDS) form.elements[field].value = saved(c)[field];
    picker.set(c.timezone);
    select.value = c.vapi_assistant_id;
    shown = key;
  }
  update();
}

onSelect(() => { zone.open = false; setStatus(status, "", ""); sync(true); });  // collapsed again for every customer
onLists((event) => {
  if (event.state !== "ready") return;
  const c = selectedCustomer();
  // dropdowns.js has just refilled the select (runs first): show the saved assistant if nothing is picked.
  if (event.list === "vapi" && c && !select.value) select.value = c.vapi_assistant_id;
  sync(false);
});
form.addEventListener("input", () => { setStatus(status, "", ""); update(); });
form.addEventListener("change", update);

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const c = selectedCustomer();
  if (!c) return;
  const body = changes(c);
  if (!Object.keys(body).length) return;
  save.disabled = true;
  setStatus(status, "", "Saving…");
  try {
    const { res, data } = await postJson(api, "/api/customers/" + encodeURIComponent(c.id), body);
    if (res.ok) {
      setStatus(status, "", "");
      const moved = data.callbacks_moved ? ` ${data.callbacks_moved} pending callback(s) moved to the new time zone.` : "";
      toast(`${data.first_name} ${data.last_name} saved.${moved}`);
      loadCustomers();
    } else {
      setStatus(status, "err", failedText(res, data));
      update();
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
    update();
  }
});
