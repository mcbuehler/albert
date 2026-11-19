// Shared UI helpers.

// Show a status message: kind "" (neutral), "ok" or "err".
export function setStatus(el, kind, text) {
  el.className = kind ? "status " + kind : "status";
  el.textContent = text;
}

export function failedText(res, data) {
  return res.status === 401 ? "Failed: please sign in again." : "Failed: " + (data.error || data.message || res.status);
}

// POST JSON with the signed-in user's login; returns { res, data }.
export async function postJson(api, path, body) {
  const res = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { res, data };
}

// Short-lived message in the corner for finished actions (errors stay longer). Click to dismiss.
export function toast(text, kind = "ok") {
  const item = Object.assign(document.createElement("div"), { className: "toast " + kind, textContent: text });
  const remove = () => item.remove();
  item.addEventListener("click", remove);
  document.getElementById("toasts").append(item);
  setTimeout(remove, kind === "err" ? 10000 : 5000);
}
