// "+ New customer" dialog. After saving, the new customer is selected in the panel.

import { api } from "./auth.js";
import { selectCustomer } from "./selection.js";
import { loadCustomers } from "./store.js";
import { createTimezonePicker } from "./timezone-picker.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const dialog = document.getElementById("customer-dialog");
const form = document.getElementById("customer-form");
const status = document.getElementById("customer-status");
const picker = createTimezonePicker({
  search: document.getElementById("tz-search"),
  list: document.getElementById("tz-list"),
  input: form.elements.timezone,
});
picker.ready.then(() => { if (!picker.value()) picker.set(Intl.DateTimeFormat().resolvedOptions().timeZone); });

document.getElementById("new-customer-btn").addEventListener("click", () => {
  setStatus(status, "", "");
  dialog.showModal();
  form.elements.first_name.focus();
});
dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!picker.value()) {
    setStatus(status, "err", "Please pick a time zone from the list.");
    picker.focus();
    return;
  }
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  setStatus(status, "", "Saving…");
  try {
    const { res, data } = await postJson(api, "/api/customers", Object.fromEntries(new FormData(form)));
    if (res.ok) {
      for (const field of ["first_name", "middle_name", "last_name", "phone"]) form.elements[field].value = "";
      dialog.close();
      toast(`Added ${data.first_name} ${data.last_name}.`);
      selectCustomer(data.id);  // shown as soon as the list has reloaded
      loadCustomers();
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
});
