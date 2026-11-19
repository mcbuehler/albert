// "Call now" dialog for the selected customer (opened from the panel).

import { api } from "./auth.js";
import { selectedCustomer } from "./selection.js";
import { loadCustomers } from "./store.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const dialog = document.getElementById("call-dialog");
const form = document.getElementById("call-form");
const status = document.getElementById("call-status");

document.getElementById("open-call").addEventListener("click", () => {
  const customer = selectedCustomer();
  if (!customer) return;
  form.reset();
  setStatus(status, "", "");
  document.getElementById("call-dialog-title").textContent = `Call ${customer.first_name} ${customer.last_name} now`;
  dialog.showModal();
  form.querySelector("button[type=submit]").focus();
});
dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const customer = selectedCustomer();
  if (!customer) return;
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  setStatus(status, "", "Starting call…");
  try {
    const fields = { ...Object.fromEntries(new FormData(form)), customer_phone_number: customer.phone };
    const { res, data } = await postJson(api, "/api/tools/call", fields);
    if (res.ok) {
      dialog.close();
      toast(`Calling ${customer.first_name} ${customer.last_name}…`);
      loadCustomers();  // shows "Calling now"
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
});
