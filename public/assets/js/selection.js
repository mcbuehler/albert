// The customer shown in the detail panel (by id, so it survives list reloads).

import { customersById } from "./store.js";

const subscribers = [];
let selectedId = null;

export function onSelect(fn) {
  subscribers.push(fn);
}

export function selectCustomer(id) {
  selectedId = id;
  for (const fn of subscribers) fn(id);
}

export function selectedCustomerId() {
  return selectedId;
}

// The selected customer's latest data, or null (none selected, or not loaded yet).
export function selectedCustomer() {
  return selectedId ? customersById.get(selectedId) || null : null;
}
