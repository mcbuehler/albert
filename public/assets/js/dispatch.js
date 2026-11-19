// Manual dispatcher run (Debug tab): same as the every-minute cron, authorized with the signed-in user.

import { api } from "./auth.js";
import { loadCustomers } from "./store.js";
import { failedText, setStatus, toast } from "./ui.js";

const button = document.getElementById("dispatch-btn");
const status = document.getElementById("dispatch-status");

button.addEventListener("click", async () => {
  button.disabled = true;
  setStatus(status, "", "Running dispatcher…");
  try {
    const res = await api("/api/cron/dispatch");
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      const started = (data.dispatched || []).filter((d) => d.vapi_call_id).length;
      const failed = (data.dispatched || []).length - started;
      loadCustomers();
      setStatus(status, "", "");
      toast(started || failed
        ? "Started " + started + " call(s)" + (failed ? ", " + failed + " could not start (will retry)." : ".")
        : "Nothing to dispatch: no callback is due, it's outside calling hours, or the phone number is busy.",
      failed ? "err" : "ok");
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  } finally {
    button.disabled = false;
  }
});
