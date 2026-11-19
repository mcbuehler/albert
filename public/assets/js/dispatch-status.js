// "Last dispatch" line in the Debug tab: when the dispatcher (Vercel Cron every minute, or
// "Run dispatcher now") last ran. Amber if it failed or hasn't run for a while (cron not running?).

import { api } from "./auth.js";
import { onLists } from "./store.js";

const STALE_MINUTES = 3;  // the cron runs every minute
const line = document.getElementById("dispatch-info");
let status = null;

function ago(minutes) {
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.floor(minutes / 60)} h ago`;
  return `${Math.floor(minutes / 1440)} days ago`;
}

function render() {
  if (!status) {
    line.textContent = "";
    line.classList.remove("warn");
    return;
  }
  if (!status.last_run_at) {
    line.textContent = "The dispatcher hasn't run yet.";
    line.classList.add("warn");
    return;
  }
  const at = new Date(status.last_run_at);
  const minutes = Math.floor((Date.now() - at.getTime()) / 60000);
  const time = at.toLocaleString("en-GB", minutes < 24 * 60
    ? { hour: "2-digit", minute: "2-digit" }
    : { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const parts = [`Last dispatch: ${time} (${ago(minutes)})`];
  if (status.last_started) parts.push(`started ${status.last_started} call${status.last_started === 1 ? "" : "s"}`);
  if (status.last_run_ok === false) parts.push("the run failed, see the logs");
  else if (minutes >= STALE_MINUTES) parts.push("the cron may not be running");
  line.textContent = parts.join(" · ");
  line.classList.toggle("warn", status.last_run_ok === false || minutes >= STALE_MINUTES);
}

async function load() {
  try {
    const res = await api("/api/dispatcher/status");
    if (!res.ok) return;  // keep the last known status
    status = await res.json();
    render();
  } catch {
    // network hiccup: keep the last known status
  }
}

// Reload with every customer refresh (auto-refresh, after actions, after "Run dispatcher now").
onLists((event) => {
  if (event.list !== "customers") return;
  if (event.state === "ready") load();
  else if (event.state === "signedOut") { status = null; render(); }
});

// And whenever the Debug tab is opened.
document.querySelector('.tabs [data-view="debug-view"]').addEventListener("click", load);

setInterval(render, 30000);  // keep "x min ago" current between refreshes
