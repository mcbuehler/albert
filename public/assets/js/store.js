// Customers (from our database) and Vapi phone numbers / assistants (from Vapi), loaded independently,
// so one failing doesn't hide the other.
// Subscribers get { list: "customers" | "vapi", state: "loading" | "ready" | "error" | "signedOut", data, error }.

import { api, isSignedIn } from "./auth.js";

const subscribers = [];
const requests = { customers: 0, vapi: 0 };
let vapiLoaded = false;
export let customers = [];
export let customersByPhone = new Map();
export let customersById = new Map();

export function onLists(fn) {
  subscribers.push(fn);
}

function notify(event) {
  for (const fn of subscribers) fn(event);
}

// GET a list; notifies "ready" with the data or "error" with a message. Returns the data or null.
async function load(list, path, showLoading) {
  const request = ++requests[list];
  if (showLoading) notify({ list, state: "loading" });
  try {
    const res = await api(path);
    const data = await res.json().catch(() => ({}));
    if (request !== requests[list] || !isSignedIn()) return null;  // a newer load started, or signed out
    if (res.ok) {
      notify({ list, state: "ready", data });
      return data;
    }
    if (res.status !== 401) notify({ list, state: "error", error: "Could not load: " + (data.error || res.status) });  // 401: api() showed the login
  } catch (err) {
    if (request === requests[list]) notify({ list, state: "error", error: "Could not load: " + err.message });
  }
  return null;
}

// Customers only: after scheduling, cancelling, calling or adding a customer.
export function loadCustomers() {
  return load("customers", "/api/customers", !customers.length);
}

// Both lists: on sign-in and Refresh.
export function loadLists() {
  loadCustomers();
  load("vapi", "/api/vapi/options", !vapiLoaded);
}

// Forget everything (on sign-out), so no customer data stays in the page.
export function clearLists() {
  requests.customers++;
  requests.vapi++;
  vapiLoaded = false;
  setCustomers([]);
  notify({ list: "customers", state: "signedOut" });
  notify({ list: "vapi", state: "signedOut" });
}

function setCustomers(list) {
  customers = list;
  customersByPhone = new Map(list.map((c) => [c.phone, c]));
  customersById = new Map(list.map((c) => [c.id, c]));
}

// Keep the lookups in sync before other subscribers render (this subscriber is registered first).
onLists((event) => {
  if (event.state !== "ready") return;
  if (event.list === "customers") setCustomers(event.data);
  else vapiLoaded = true;
});
