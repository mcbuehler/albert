// Formatting of customers, Vapi items, call outcomes and times.

// Vapi endedReason -> plain words for staff.
const OUTCOMES = {
  "customer-did-not-answer": "No answer",
  "customer-busy": "Busy",
  "voicemail": "Voicemail",
  "silence-timed-out": "Silence",
  "customer-ended-call": "Answered",
  "assistant-ended-call": "Answered",
  "assistant-said-end-call-phrase": "Answered",
  "assistant-ended-call-after-message-spoken": "Answered",
  "assistant-ended-call-with-hangup-task": "Answered",
  "assistant-forwarded-call": "Forwarded",
  "exceeded-max-duration": "Answered (max duration)",
  "dispatch-error": "Could not start",
  "unknown": "Unknown outcome",
};

export function outcomeLabel(reason) {
  if (!reason) return "";
  return OUTCOMES[reason] || (/error|failed/i.test(reason) ? "Error" : reason);
}

// Format an ISO time (or now, if iso is null) in a time zone.
export function inZone(iso, zone, options) {
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: zone, ...options }).format(iso ? new Date(iso) : new Date());
  } catch {
    return "";
  }
}

export const whenFormat = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };

// "First Last (+417912...)": the phone is shortened, like in the logs.
export function customerLabel(c) {
  return c.first_name + " " + c.last_name + " (" + c.phone.slice(0, 7) + "...)";
}

// "Name (abcd...)" for Vapi phone numbers and assistants.
export function vapiLabel(item) {
  return item.name + " (" + item.id.slice(0, 4) + "...)";
}

// Overall state of a customer, for the status badge and the list order.
export function statusOf(c) {
  if (c.opted_out) return { key: "opted-out", label: "Opted out", order: 3 };
  if (c.calling_now) return { key: "calling", label: "Calling now", order: 1 };
  if (c.pending_callbacks > 0) return { key: "scheduled", label: "Scheduled", order: 2 };
  return { key: "nothing", label: "Nothing scheduled", order: 0 };
}

// --- Customer-local times as naive "YYYY-MM-DDTHH:MM" strings (what the schedule form sends) ---

const pad = (n) => String(n).padStart(2, "0");

function naiveFromUtcDate(d) {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

// Arithmetic on naive local times: treat them as UTC so DST doesn't interfere.
function naiveToUtcDate(naive) {
  return new Date(naive + ":00Z");
}

// The current time in a zone, e.g. "2026-10-17T14:05".
export function localNow(zone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function addMinutes(naive, minutes) {
  return naiveFromUtcDate(new Date(naiveToUtcDate(naive).getTime() + minutes * 60000));
}

// Same day at a "HH:MM" time, `days` later.
export function atTime(naive, days, hhmm) {
  return addMinutes(naive.slice(0, 10) + "T00:00", days * 1440).slice(0, 11) + hhmm;
}

// "Tue 18 Oct, 09:00" for a naive time.
export function naiveLabel(naive) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", ...whenFormat }).format(naiveToUtcDate(naive));
}

// --- Calls tab ---

export const CALL_STATUS_LABELS = {
  scheduled: "Scheduled", calling: "Calling", completed: "Completed", no_answer: "No answer",
  voicemail: "Voicemail", failed: "Failed", cancelled: "Cancelled",
};
export const DIRECTION_LABELS = { inbound: "Inbound", outbound: "Outbound", web: "Web", unknown: "Unknown" };

/** "Thu 5 Nov 2026, 14:05" in the browser's time zone. @param {string} iso @returns {string} */
export function callDate(iso) {
  return new Intl.DateTimeFormat("en-GB", { ...whenFormat, year: "numeric" }).format(new Date(iso));
}

/** "4:05" or "1:02:03" for a duration in seconds, "–" when unknown. @param {number|null} seconds @returns {string} */
export function durationLabel(seconds) {
  if (seconds == null) return "–";
  const s = Math.round(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

/** Status badge of a Calls entry. @param {string} status @returns {HTMLSpanElement} */
export function statusBadge(status) {
  return Object.assign(document.createElement("span"), { className: "badge " + status, textContent: CALL_STATUS_LABELS[status] || status });
}
