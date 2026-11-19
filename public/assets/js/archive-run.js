// Manual archive run (Debug tab): same as the every-minute cron, authorized with the signed-in user.

import { api } from "./auth.js";
import { loadArchiveStatus } from "./archive-status.js";
import { failedText, setStatus, toast } from "./ui.js";

const button = document.getElementById("archive-btn");
const status = document.getElementById("archive-status");

/** Summarize one run's counts for the toast. @param {Object} data @returns {string} */
function summary(data) {
  if (data.busy) return "Another archive run is in progress; try again in a minute.";
  const parts = [`${data.saved} new`, `${data.refreshed} updated`, `${data.audio} recordings copied`];
  if (data.audio_failed) parts.push(`${data.audio_failed} recordings failed (will retry)`);
  if (data.cleaned) parts.push(`${data.cleaned} recordings deleted`);
  return "Archive run: " + parts.join(", ") + ".";
}

/** Run the archive once and refresh the status line. @returns {Promise<void>} */
async function run() {
  button.disabled = true;
  setStatus(status, "", "Running archive…");
  try {
    const res = await api("/api/cron/archive");
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      setStatus(status, "", "");
      toast(summary(data), data.audio_failed ? "err" : "ok");
    } else {
      setStatus(status, "err", failedText(res, data));
    }
  } catch (err) {
    setStatus(status, "err", "Failed: " + err.message);
  } finally {
    button.disabled = false;
    loadArchiveStatus();
  }
}

button.addEventListener("click", run);
