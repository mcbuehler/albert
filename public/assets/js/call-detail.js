// Call detail dialog (Calls tab): details, recording, transcript and staff notes of one entry.
// Recordings play from a 5-minute Supabase link; an expired link is renewed at the same position.
import { api } from "./auth.js";
import { callDate, DIRECTION_LABELS, durationLabel, outcomeLabel, statusBadge } from "./format.js";
import { postJson, setStatus } from "./ui.js";

const RENEW_BEFORE_MS = 20000;  // renew a link this long before it expires
const TRANSCRIPT_NOTES = {
  pending: "The transcript isn't available yet.", empty: "Nobody spoke in this call.",
  disabled: "Transcripts were turned off for this call.", unavailable: "The transcript couldn't be saved.",
};
const AUDIO_NOTES = {
  pending: "The recording is still being saved.", disabled: "Recording was turned off for this call.",
  unavailable: "The recording couldn't be saved.", not_applicable: "This call has no recording.",
};

const dialog = document.getElementById("call-detail");
const title = document.getElementById("call-detail-title");
const status = document.getElementById("call-detail-status");
const body = document.getElementById("call-detail-body");
const meta = document.getElementById("call-detail-meta");
const audio = document.getElementById("call-detail-audio");
const audioNote = document.getElementById("call-detail-audio-note");
const transcript = document.getElementById("call-detail-transcript");
const transcriptNote = document.getElementById("call-detail-transcript-note");
const notes = document.getElementById("call-detail-notes");
const notesNote = document.getElementById("call-detail-notes-note");
const noteForm = document.getElementById("call-note-form");
const noteStatus = document.getElementById("call-note-status");
let generation = 0;
let entryId = null;
let expiresAt = 0;
let opener = null;

/** Add one term/value pair to the details list. @param {string} term @param {string|Node} value @returns {void} */
function detail(term, value) {
  const dd = document.createElement("dd");
  dd.append(value);
  meta.append(Object.assign(document.createElement("dt"), { textContent: term }), dd);
}

/** Fill the details list for a call or callback. @param {Object} call @returns {void} */
function renderMeta(call) {
  meta.replaceChildren();
  detail("Number", call.phone || "Number withheld");
  detail(call.kind === "scheduled" ? "Scheduled for" : "Date", callDate(call.call_date));
  detail("Status", statusBadge(call.status));
  if (call.ended_reason) detail("Ended", `${outcomeLabel(call.ended_reason)} (${call.ended_reason})`);
  detail("Direction", DIRECTION_LABELS[call.direction] || call.direction);
  if (call.kind !== "scheduled") detail("Duration", durationLabel(call.duration_seconds));
  if (call.instructions) detail("Instructions", call.instructions);
}

/** Show speaker-labelled messages, or the reason there are none. @param {Object} call @returns {void} */
function renderTranscript(call) {
  transcript.replaceChildren();
  for (const m of call.messages) {
    const li = document.createElement("li");
    li.className = m.role === "user" ? "customer" : "assistant";
    li.append(Object.assign(document.createElement("strong"), { textContent: m.role === "user" ? "Customer" : "Albert" }), " ", m.message);
    transcript.append(li);
  }
  const fallback = !call.messages.length && call.transcript;
  if (fallback) transcript.append(Object.assign(document.createElement("li"), { textContent: call.transcript }));
  transcriptNote.textContent = call.messages.length || fallback ? "" : TRANSCRIPT_NOTES[call.transcript_state] || "No transcript.";
}

/** Append one staff note; callback discussion is labelled. @param {Object} note @returns {void} */
function renderNote(note) {
  const li = document.createElement("li");
  const by = `${note.author_label} · ${callDate(note.created_at)}${note.target === "callback" ? " · on the callback" : ""}`;
  li.append(Object.assign(document.createElement("span"), { className: "sub", textContent: by }),
    Object.assign(document.createElement("p"), { textContent: note.body }));
  notes.append(li);
  notesNote.textContent = "";
}

/** Show staff notes oldest first. @param {Object[]} list @returns {void} */
function renderNotes(list) {
  notes.replaceChildren();
  list.forEach(renderNote);
  notesNote.textContent = list.length ? "" : "No notes yet.";
}

/** Save the typed note and show it; the draft stays if saving fails. @param {SubmitEvent} event @returns {Promise<void>} */
async function addNote(event) {
  event.preventDefault();
  const request = generation, button = noteForm.querySelector("button");
  button.disabled = true;
  setStatus(noteStatus, "", "Saving…");
  try {
    const { res, data } = await postJson(api, "/api/calls/" + encodeURIComponent(entryId) + "/notes", { body: noteForm.body.value });
    if (request !== generation) return;
    if (!res.ok) return setStatus(noteStatus, "err", data.error || "The note couldn't be saved.");
    entryId = data.entry_id;  // a note on an attempt gives it a durable call record
    renderNote(data.note);
    noteForm.reset();
    setStatus(noteStatus, "", "");
  } catch (err) {
    if (request === generation) setStatus(noteStatus, "err", "The note couldn't be saved: " + err.message);
  } finally {
    button.disabled = false;
  }
}

/** Load a fresh playback link and continue at `position`, playing again if it was playing. @param {number} request @param {number} position @param {boolean} resume @returns {Promise<void>} */
async function loadAudio(request, position = 0, resume = false) {
  const { res, data } = await postJson(api, "/api/calls/" + encodeURIComponent(entryId) + "/audio-url").catch(() => ({ res: null, data: {} }));
  if (request !== generation) return;
  if (!res?.ok) {
    audio.hidden = true;
    audioNote.textContent = "The recording can't be played right now: " + (data.error || "please try again.");
    return;
  }
  expiresAt = Date.now() + data.expires_in * 1000;
  audio.src = data.url;
  audio.hidden = false;
  audioNote.textContent = "";
  if (position) audio.addEventListener("loadedmetadata", () => { audio.currentTime = position; }, { once: true });
  if (resume) audio.play().catch(() => {});
}

/** Renew the link when it is about to expire (or did), keeping the playback position. @returns {boolean} whether it renews */
function renewIfExpiring() {
  if (!entryId || !audio.src || Date.now() < expiresAt - RENEW_BEFORE_MS) return false;
  expiresAt = Infinity;  // one renewal at a time
  loadAudio(generation, audio.currentTime, !audio.paused);
  return true;
}

/** Show a loaded entry. @param {Object} call @returns {void} */
function render(call) {
  entryId = call.id;  // the current ID, if the list's one was merged or archived since
  title.textContent = call.name || "Unknown caller";
  renderMeta(call);
  renderTranscript(call);
  renderNotes(call.notes);
  document.getElementById("call-detail-transcript-box").hidden = call.kind === "scheduled";
  document.getElementById("call-detail-audio-box").hidden = call.kind === "scheduled";
  audio.hidden = true;
  audioNote.textContent = call.audio_state === "ready" ? "Loading the recording…" : AUDIO_NOTES[call.audio_state] || "No recording.";
  if (call.audio_state === "ready") loadAudio(generation);
  setStatus(status, "", "");
  body.hidden = false;
}

/**
 * Open the dialog for one Calls entry and load its details; focus returns to the opener on close.
 * @param {string} id Typed entry ID from the Calls list.
 * @returns {Promise<void>}
 */
export async function openCall(id) {
  const request = ++generation;
  opener = document.activeElement;
  entryId = id;
  title.textContent = "Call";
  body.hidden = true;
  setStatus(status, "", "Loading…");
  dialog.showModal();
  try {
    const res = await api("/api/calls/" + encodeURIComponent(id));
    const data = await res.json().catch(() => ({}));
    if (request !== generation) return;
    if (res.ok) render(data);
    else setStatus(status, "err", data.error ? (res.status === 404 ? data.error : "Could not load the call: " + data.error)
      : `Could not load the call (HTTP ${res.status}). Is the backend up to date?`);
  } catch (err) {
    if (request === generation) setStatus(status, "err", "Could not load the call: " + err.message);
  }
}

/** Stop playback and forget the call's content when the dialog closes (also on logout). @returns {void} */
function clear() {
  generation++;
  entryId = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  for (const list of [meta, transcript, notes]) list.replaceChildren();
  noteForm.reset();
  setStatus(noteStatus, "", "");
  opener?.focus?.();
  opener = null;
}

dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
// A click on the backdrop targets the dialog itself but lies outside its box.
dialog.addEventListener("click", (e) => {
  const box = dialog.getBoundingClientRect();
  const outside = e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom;
  if (e.target === dialog && outside) dialog.close();
});
dialog.addEventListener("close", clear);
noteForm.addEventListener("submit", addNote);
audio.addEventListener("play", renewIfExpiring);
audio.addEventListener("seeking", renewIfExpiring);
audio.addEventListener("error", () => {
  if (!audio.src || renewIfExpiring()) return;
  audioNote.textContent = "The recording couldn't be played.";
});
