// Danger zone in the panel: delete a customer or erase their memory.
// Both open one confirmation dialog; the customer's phone number must be typed to enable the button
// (the server checks it again).

import { api } from "./auth.js";
import { onSelect, selectCustomer, selectedCustomer } from "./selection.js";
import { loadCustomers } from "./store.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const zone = document.getElementById("panel-danger");
const dialog = document.getElementById("danger-dialog");
const form = document.getElementById("danger-form");
const input = form.elements.confirm_phone;
const submit = form.querySelector("button[type=submit]");
const status = document.getElementById("danger-status");

const ACTIONS = {
  erase: {
    title: (name) => `Erase ${name}'s memory`,
    text: "Albert forgets everything from earlier calls with this customer. The customer, their callbacks, call history, "
      + "recordings and transcripts stay.",
    button: "Erase memory",
    post: (id, body) => postJson(api, "/api/customers/" + encodeURIComponent(id) + "/erase-memory", body),
    done: (name) => `${name}'s memory was erased.`,
  },
  delete: {
    title: (name) => `Delete ${name}`,
    text: "Deletes the customer with their memory, pending callbacks, call history, recordings, transcripts and staff notes.",
    button: "Delete customer",
    post: (id, body) => postJson(api, "/api/customers/" + encodeURIComponent(id) + "/delete", body),
    done: (name) => `${name} was deleted.`,
  },
};

let action = null;

// Same idea as the server's normalize_phone: spaces, dashes, dots, slashes, brackets and a leading 00 don't matter.
const normalized = (text) => text.replace(/[\s\-./()]/g, "").replace(/^00/, "+");

function matches() {
  const customer = selectedCustomer();
  return Boolean(customer) && normalized(input.value) === customer.phone;
}

function open(kind) {
  const customer = selectedCustomer();
  if (!customer) return;
  action = ACTIONS[kind];
  const name = `${customer.first_name} ${customer.last_name}`;
  form.reset();
  setStatus(status, "", "");
  document.getElementById("danger-dialog-title").textContent = action.title(name);
  document.getElementById("danger-text").textContent = action.text;
  document.getElementById("danger-phone").textContent = customer.phone;
  submit.textContent = action.button;
  submit.disabled = true;
  dialog.showModal();
  input.focus();
}

document.getElementById("open-erase").addEventListener("click", () => open("erase"));
document.getElementById("open-delete").addEventListener("click", () => open("delete"));
dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
input.addEventListener("input", () => { submit.disabled = !matches(); });
onSelect(() => { zone.open = false; });  // collapsed again for every customer

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const customer = selectedCustomer();
  if (!customer || !matches()) return;
  const current = action;
  const name = `${customer.first_name} ${customer.last_name}`;
  submit.disabled = true;
  setStatus(status, "", "Working… erasing the memory can take a minute or two. Please keep this open.");
  try {
    const { res, data } = await current.post(customer.id, { confirm_phone: input.value });
    if (res.ok) {
      dialog.close();
      toast(current.done(name));
      if (current === ACTIONS.delete) selectCustomer(null);
      loadCustomers();
    } else {
      setStatus(status, "err", failedText(res, data));
      submit.disabled = !matches();
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
    submit.disabled = !matches();
  }
});
