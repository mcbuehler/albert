// Keep the customer list current without a Refresh button: every 15 s while a call is running,
// otherwise every 60 s. Paused while the tab is hidden; refreshes as soon as it's visible again.

import { isSignedIn } from "./auth.js";
import { customers, loadCustomers, onLists } from "./store.js";

const FAST_MS = 15000;
const SLOW_MS = 60000;
let timer = null;

function schedule() {
  clearTimeout(timer);
  timer = null;
  if (!isSignedIn() || document.hidden) return;
  timer = setTimeout(loadCustomers, customers.some((c) => c.calling_now) ? FAST_MS : SLOW_MS);
}

// Every load (automatic or after an action) restarts the countdown.
onLists((event) => {
  if (event.list !== "customers") return;
  if (event.state === "signedOut") { clearTimeout(timer); timer = null; }
  else if (event.state !== "loading") schedule();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearTimeout(timer); timer = null; }
  else if (isSignedIn()) loadCustomers();
});
