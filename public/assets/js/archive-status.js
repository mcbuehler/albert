// Call archive status line (Debug tab): scans, sweep progress and recordings.
import { api } from "./auth.js";
import { onLists } from "./store.js";

/**
 * @typedef {Object} ArchiveStatus
 * @property {boolean} enabled
 * @property {number} [calls]
 * @property {number} [pending_audio]
 * @property {number} [retrying_audio]
 * @property {number} [unavailable_audio]
 * @property {number} [pending_cleanup]
 * @property {string|null} [oldest_pending_at]
 * @property {{last_succeeded_at: string|null, pending_windows: number, consecutive_failures: number}|null} [scan]
 * @property {{completed_through: string|null, consecutive_failures: number}|null} [reconcile]
 */

const line = document.getElementById("archive-info");
let generation = 0;

/** Render scan diagnostics without loading call content. @param {ArchiveStatus} data @returns {void} */
function renderStatus(data) {
  line.classList.remove("warn");
  if (!data.enabled) {
    line.textContent = "Call archival is not enabled yet.";
    return;
  }
  const scan = data.scan;
  const last = scan?.last_succeeded_at;
  const parts = [last ? `Last call scan: ${new Date(last).toLocaleString()}` : "No completed call scan yet",
    `${data.calls} calls saved`, `${data.pending_audio} recordings pending`];
  if (data.retrying_audio) parts.push(`${data.retrying_audio} recordings retrying`);
  if (data.unavailable_audio) parts.push(`${data.unavailable_audio} recordings unavailable`);
  if (data.pending_cleanup) parts.push(`${data.pending_cleanup} recordings awaiting deletion`);
  if (data.oldest_pending_at) parts.push(`oldest pending: ${new Date(data.oldest_pending_at).toLocaleString()}`);
  if (scan?.pending_windows) parts.push(`${scan.pending_windows} scan windows pending`);
  if (scan?.consecutive_failures) parts.push(`${scan.consecutive_failures} failed runs`);
  const sweep = data.reconcile;
  if (sweep?.completed_through) parts.push(`older calls checked through ${new Date(sweep.completed_through).toLocaleString()}`);
  if (sweep?.consecutive_failures) parts.push(`${sweep.consecutive_failures} failed sweeps`);
  const stale = !last || Date.now() - new Date(last).getTime() > 180000;
  line.classList.toggle("warn", stale || Boolean(scan?.consecutive_failures || sweep?.consecutive_failures));
  line.textContent = parts.join(" · ");
}

/** Fetch the current status and discard responses superseded by logout or a newer load. @returns {Promise<void>} */
export async function loadArchiveStatus() {
  const requestGeneration = ++generation;
  try {
    const res = await api("/api/archive/status");
    if (requestGeneration !== generation) return;
    if (!res.ok) throw new Error("status unavailable");
    const data = await res.json();
    if (requestGeneration !== generation) return;
    renderStatus(data);
  } catch {
    if (requestGeneration !== generation) return;
    line.textContent = "Call archive status could not be loaded.";
    line.classList.add("warn");
  }
}

/** Refresh after customer loads and clear diagnostics on logout. @param {{list: string, state: string}} event @returns {void} */
function handleLists(event) {
  if (event.list !== "customers") return;
  if (event.state === "ready") loadArchiveStatus();
  else if (event.state === "signedOut") {
    generation++;
    line.textContent = "";
    line.classList.remove("warn");
  }
}

onLists(handleLists);
document.querySelector('.tabs [data-view="debug-view"]').addEventListener("click", loadArchiveStatus);
