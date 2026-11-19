// Calls tab: archived calls, attempts and upcoming callbacks, searched, filtered, sorted and paged on the server.
import { api, isSignedIn } from "./auth.js";
import { callDate, DIRECTION_LABELS, durationLabel, statusBadge } from "./format.js";
import { openCall } from "./call-detail.js";
import { onLists } from "./store.js";

/**
 * @typedef {Object} CallEntry
 * @property {string} id Typed entry ID ("call:", "attempt:" or "scheduled:" + UUID).
 * @property {string|null} phone
 * @property {string|null} name
 * @property {string} call_date
 * @property {string} status
 * @property {string} direction
 * @property {number|null} duration_seconds
 * @property {string|null} ended_reason
 * @property {string|null} transcript_state
 * @property {string|null} audio_state
 * @property {number} note_count
 */

const CALLS_PATH = "/api/calls";
const PAGE_SIZE = 50;
const REFRESH_MS = 30000;

const view = document.getElementById("calls-view");
const rows = document.getElementById("calls-rows");
const table = document.getElementById("calls-table");
const search = document.getElementById("calls-search");
const filter = document.getElementById("calls-filter");
const range = document.getElementById("calls-range");
const prev = document.getElementById("calls-prev");
const next = document.getElementById("calls-next");
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const state = { sort: "call_date", order: "desc", offset: 0 };
let generation = 0;
let debounce = null;
let timer = null;

document.getElementById("calls-zone").textContent = `Times in your time zone (${zone}).`;

/** Replace the table body with a single message row. @param {string} text @returns {void} */
function message(text) {
  rows.replaceChildren();
  const td = rows.insertRow().insertCell();
  td.colSpan = 6;
  td.textContent = text;
}

/** Append a cell with text and an optional grey second line. @param {HTMLTableRowElement} row @param {string|Node} content @param {string} [sub] @returns {HTMLTableCellElement} */
function cell(row, content, sub) {
  const td = row.insertCell();
  td.append(content);
  if (sub) td.append(Object.assign(document.createElement("span"), { className: "sub", textContent: sub }));
  return td;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const ICON_PATHS = {  // 16×16 outlines drawn with currentColor
  audio: "M2 6h3l4-3v10l-4-3H2z M11 5.5a3.5 3.5 0 0 1 0 5 M12.5 3.5a6 6 0 0 1 0 9",
  transcript: "M3 1.5h7l3 3v10H3z M5 6.5h6 M5 9h6 M5 11.5h4",
};
const MEDIA_TITLES = {
  audio: { ready: "Recording available", pending: "Recording still being saved", none: "No recording" },
  transcript: { ready: "Transcript available", pending: "Transcript not available yet", none: "No transcript" },
};

/** Icon showing whether a recording or transcript is available (dimmed if not). @param {"audio"|"transcript"} kind @param {string|null} value @returns {SVGSVGElement} */
function mediaIcon(kind, value) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "media-icon " + (value === "ready" ? "ready" : "missing"));
  svg.setAttribute("role", "img");
  const label = MEDIA_TITLES[kind][value] || MEDIA_TITLES[kind].none;
  svg.setAttribute("aria-label", label);
  const title = document.createElementNS(SVG_NS, "title");
  title.textContent = label;
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICON_PATHS[kind]);
  svg.append(title, path);
  return svg;
}

/** Status badge plus recording/transcript icons (actual calls only). @param {CallEntry} call @returns {HTMLSpanElement} */
function statusCell(call) {
  const span = Object.assign(document.createElement("span"), { className: "badges" });
  span.append(statusBadge(call.status));
  if (!call.id.startsWith("scheduled:")) span.append(mediaIcon("audio", call.audio_state), mediaIcon("transcript", call.transcript_state));
  return span;
}

/** Thick "now" line between upcoming and past entries. @returns {void} */
function nowLine() {
  const row = rows.insertRow();
  row.className = "now-line";
  const td = row.insertCell();
  td.colSpan = 6;
  const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" }).format(new Date());
  td.textContent = state.order === "desc" ? `Now, ${time} · upcoming above, past below` : `Now, ${time} · past above, upcoming below`;
}

/** Whether an entry lies on the side shown first (upcoming when newest first). @param {CallEntry} call @param {number} now @returns {boolean} */
function beforeNow(call, now) {
  return (new Date(call.call_date).getTime() > now) === (state.order === "desc");
}

/** Render one entry as a table row that opens the call's details. @param {CallEntry} call @returns {void} */
function renderRow(call) {
  const row = rows.insertRow();
  row.dataset.id = call.id;
  row.tabIndex = 0;
  row.addEventListener("click", () => openCall(call.id));
  row.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    openCall(call.id);
  });
  cell(row, call.phone || "Number withheld");
  cell(row, call.name || "Unknown caller", call.note_count ? `${call.note_count} note${call.note_count === 1 ? "" : "s"}` : "");
  cell(row, callDate(call.call_date));
  cell(row, statusCell(call)).title = call.ended_reason || "";
  cell(row, DIRECTION_LABELS[call.direction] || call.direction);
  cell(row, durationLabel(call.duration_seconds));
}

/** Render a page; sorted by date, a line marks now (also at the page edge if it falls there). @param {{calls: CallEntry[], total: number}} data @returns {void} */
function renderRows(data) {
  const now = Date.now(), byDate = state.sort === "call_date", lastPage = state.offset + data.calls.length >= data.total;
  rows.replaceChildren();
  data.calls.forEach((call, i) => {
    const previous = i ? beforeNow(data.calls[i - 1], now) : state.offset === 0;
    if (byDate && previous && !beforeNow(call, now)) nowLine();
    renderRow(call);
  });
  if (byDate && lastPage && beforeNow(data.calls[data.calls.length - 1], now)) nowLine();
}

/** Show a loaded page and update the pager. @param {{calls: CallEntry[], total: number}} data @returns {void} */
function render(data) {
  if (!data.calls.length && state.offset > 0) return reload();  // the list shrank below this page
  if (!data.calls.length) message(search.value.trim() || filter.value ? "No calls match." : "No calls yet.");
  else renderRows(data);
  const first = data.calls.length ? state.offset + 1 : 0;
  range.textContent = `${first}–${state.offset + data.calls.length} of ${data.total}`;
  prev.disabled = state.offset === 0;
  next.disabled = state.offset + PAGE_SIZE >= data.total;
}

/** Query string for the current search, filter, sort and page. @returns {string} */
function query() {
  const params = new URLSearchParams({ sort: state.sort, order: state.order, limit: String(PAGE_SIZE), offset: String(state.offset) });
  if (search.value.trim()) params.set("q", search.value.trim());
  if (filter.value) params.set("status", filter.value);
  return params.toString();
}

/** Show or hide the waiting indicator: a spinner row while the table is empty, else a dimmed table. @param {boolean} busy @returns {void} */
function setBusy(busy) {
  table.classList.toggle("loading", busy);
  table.setAttribute("aria-busy", String(busy));
  if (busy && !rows.querySelector("tr[data-id]")) {
    message("Loading calls…");
    rows.querySelector("td").prepend(Object.assign(document.createElement("span"), { className: "spinner" }));
  }
}

/** Load the current page, ignoring responses superseded by a newer load or logout. @param {boolean} [quiet] no indicator (background refresh) @returns {Promise<void>} */
async function load(quiet = false) {
  const request = ++generation;
  scheduleRefresh();
  if (!quiet) setBusy(true);
  try {
    const res = await api(CALLS_PATH + "?" + query());
    const data = await res.json().catch(() => ({}));
    if (request !== generation || !isSignedIn()) return;
    if (res.ok) render(data);
    else if (res.status !== 401) message("Could not load calls: " + (data.error || res.status));
  } catch (err) {
    if (request === generation) message("Could not load calls: " + err.message);
  } finally {
    if (request === generation) setBusy(false);
  }
}

/** Reload every 30 s while the Calls tab is shown, so calling and scheduled rows stay current. @returns {void} */
function scheduleRefresh() {
  clearTimeout(timer);
  timer = document.hidden || !isSignedIn() ? null : setTimeout(() => { if (!view.hidden) load(true); }, REFRESH_MS);
}

/** Start again at the first page after the search, filter or sort changed. @returns {void} */
function reload() {
  state.offset = 0;
  load();
}

/** Sort by a column; clicking the current column flips the direction. @param {HTMLButtonElement} button @returns {void} */
function sortBy(button) {
  const key = button.dataset.sort;
  const flipped = state.order === "desc" ? "asc" : "desc";
  state.order = key === state.sort ? flipped : key === "call_date" ? "desc" : "asc";  // newest dates first, A–Z otherwise
  state.sort = key;
  for (const th of view.querySelectorAll("th")) th.removeAttribute("aria-sort");
  button.parentElement.setAttribute("aria-sort", state.order === "asc" ? "ascending" : "descending");
  reload();
}

/** Forget loaded calls on logout. @param {{list: string, state: string}} event @returns {void} */
function handleLists(event) {
  if (event.list !== "customers" || event.state !== "signedOut") return;
  generation++;
  clearTimeout(timer);
  setBusy(false);
  search.value = "";
  filter.value = "";
  range.textContent = "";
  prev.disabled = next.disabled = true;
  message("Open the tab to load calls.");
}

onLists(handleLists);
document.querySelector('.tabs [data-view="calls-view"]').addEventListener("click", () => load());
for (const button of view.querySelectorAll("th .sort")) button.addEventListener("click", () => sortBy(button));
search.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(reload, 300); });
filter.addEventListener("change", reload);
prev.addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); load(); });
next.addEventListener("click", () => { state.offset += PAGE_SIZE; load(); });
document.addEventListener("visibilitychange", () => { if (!view.hidden && !document.hidden) load(); else scheduleRefresh(); });
