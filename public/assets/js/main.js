// Backend page entry point. Each module wires up its own part of the page when imported.

import { initAuth } from "./auth.js";
import { clearLists, loadLists } from "./store.js";
import { selectCustomer } from "./selection.js";
import { showView } from "./tabs.js";
import "./onboard.js";
import "./dropdowns.js";
import "./overview.js";
import "./panel.js";
import "./edit.js";
import "./review.js";
import "./schedule.js";
import "./call.js";
import "./danger.js";
import "./dispatch.js";
import "./test-email.js";
import "./refresh.js";
import "./dispatch-status.js";
import "./archive-status.js";
import "./archive-run.js";
import "./calls.js";

initAuth({
  onSignedIn: loadLists,
  onSignedOut() {
    for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
    selectCustomer(null);
    showView("customers-view");
    clearLists();
  },
});
