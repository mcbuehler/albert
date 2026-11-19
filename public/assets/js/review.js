// Panel: "Mark reviewed" for customers who signed up themselves on an inbound call (needs_review).

import { api } from "./auth.js";
import { onSelect, selectedCustomer } from "./selection.js";
import { loadCustomers } from "./store.js";
import { failedText, postJson, setStatus, toast } from "./ui.js";

const button = document.getElementById("mark-reviewed");
const status = document.getElementById("review-status");

onSelect(() => { button.disabled = false; setStatus(status, "", ""); });

button.addEventListener("click", async () => {
  const c = selectedCustomer();
  if (!c) return;
  button.disabled = true;
  setStatus(status, "", "Saving…");
  try {
    const { res, data } = await postJson(api, "/api/customers/" + encodeURIComponent(c.id) + "/reviewed");
    if (res.ok) {
      setStatus(status, "", "");
      toast(`${c.first_name} ${c.last_name} marked as reviewed.`);
      loadCustomers();
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  }
  button.disabled = false;
});
