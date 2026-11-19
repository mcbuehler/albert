// Debug tab: test email through the notify_team tool endpoint (stored, then emailed to TEAM_EMAIL by the dispatcher).

import { api } from "./auth.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const button = document.getElementById("test-email-btn");
const status = document.getElementById("test-email-status");

button.addEventListener("click", async () => {
  button.disabled = true;
  setStatus(status, "", "Sending test email…");
  try {
    const { res, data } = await postJson(api, "/api/tools/notify-team", {
      reason: "other", contact_name: "Test email", summary: "Test email from the backend page.", customer_number: null,
    });
    if (res.ok && data.result === "The team has been notified.") {
      setStatus(status, "", "");
      toast("Test email queued: the dispatcher sends it within a minute.");
    } else {
      setStatus(status, "err", res.ok ? data.result : failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  }
  button.disabled = false;
});
