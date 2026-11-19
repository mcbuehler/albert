// Searchable time zone picker (combobox), used by the onboarding form and the panel's edit zone.
// Zones come from the server list (or the browser); the server validates against IANA tzdata.
const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/_/g, " ").toLowerCase();

function offsetOf(id) {
  try {
    const part = new Intl.DateTimeFormat("en", { timeZone: id, timeZoneName: "shortOffset" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    return part.value.replace("GMT", "UTC").replace(/^UTC$/, "UTC±0");
  } catch { return ""; }
}

function genericName(id) {
  try {
    return new Intl.DateTimeFormat("en", { timeZone: id, timeZoneName: "longGeneric" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName").value;
  } catch { return ""; }
}

let zones = [];
let byId = new Map();

// Server list (zone.tab + country names + search aliases such as US states).
async function loadZones() {
  let rows;
  try {
    const res = await fetch("/api/timezones");
    if (!res.ok) throw new Error(res.status);
    rows = await res.json();
  } catch {
    rows = (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : ["UTC"]).map((id) => ({ id, country: "", keywords: "" }));
  }
  zones = rows.map(({ id, country, keywords }) => {
    const city = id.split("/").pop().replace(/_/g, " ");
    const offset = offsetOf(id);
    const generic = genericName(id);
    return {
      id, city, offset, keywords: keywords.split(/\s+/).filter(Boolean),
      label: country ? `${city}, ${country}` : city,
      base: fold(`${city} ${country} ${id} ${offset} ${generic}`),
      hay: fold(`${city} ${country} ${id} ${offset} ${generic} ${keywords}`),
    };
  }).sort((a, b) => a.city.localeCompare(b.city));
  byId = new Map(zones.map((z) => [z.id, z]));
}

const loaded = loadZones();  // once, shared by every picker

// Words from the aliases that explain a match, e.g. "San Francisco" for Los Angeles.
function aliasHint(z, terms) {
  if (terms.every((t) => z.base.includes(t))) return "";
  const words = terms.map((t) => z.keywords.find((w) => fold(w).includes(t))).filter(Boolean);
  return [...new Set(words)].join(" ");
}

function localTime(id) {
  try { return new Date().toLocaleTimeString("en-GB", { timeZone: id, hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}

// Wire up one picker: the search box (combobox), its list and the hidden input holding the IANA id.
// onChange(id) runs whenever a zone is chosen. Returns { value(), set(id), focus(), ready }.
export function createTimezonePicker({ search, list, input, onChange = () => {} }) {
  let matches = [];
  let active = -1;

  function render() {
    const terms = fold(search.value.trim()).split(/\s+/).filter(Boolean);
    const q = terms.join(" ");
    matches = zones.filter((z) => terms.every((t) => z.hay.includes(t)));
    if (q) matches.sort((a, b) => fold(b.city).startsWith(q) - fold(a.city).startsWith(q));
    active = matches.length ? 0 : -1;
    list.replaceChildren(...(matches.length ? matches.map((z, i) => {
      const li = document.createElement("li");
      li.id = list.id + "-opt-" + i;
      li.setAttribute("role", "option");
      const city = document.createElement("span");
      city.className = "tz-city";
      city.textContent = z.label;
      const hint = aliasHint(z, terms);
      if (hint) {
        const small = document.createElement("small");
        small.className = "tz-hint";
        small.textContent = "includes " + hint;
        city.append(small);
      }
      const meta = document.createElement("span");
      meta.className = "tz-meta";
      meta.textContent = `${z.offset} · ${localTime(z.id)}`;
      li.append(city, meta);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(z); });
      return li;
    }) : [Object.assign(document.createElement("li"), { className: "tz-empty", textContent: "No matching time zone" })]));
    highlight();
  }

  function highlight() {
    list.querySelectorAll("[role=option]").forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
    const el = document.getElementById(list.id + "-opt-" + active);
    if (el) { el.scrollIntoView({ block: "nearest" }); search.setAttribute("aria-activedescendant", el.id); }
    else search.removeAttribute("aria-activedescendant");
  }

  function open() { render(); list.hidden = false; search.setAttribute("aria-expanded", "true"); }
  function close() { list.hidden = true; search.setAttribute("aria-expanded", "false"); }

  // Show a zone without reporting a change (z undefined: a zone not in the list is still kept as the value).
  function show(id) {
    const z = byId.get(id);
    input.value = id || "";
    search.value = z ? `${z.label} · ${z.offset}` : id || "";
  }

  function choose(z) {
    const id = z ? z.id : "";
    const changed = id !== input.value;
    show(id);
    close();
    if (changed) onChange(id);
  }

  search.addEventListener("focus", () => { search.select(); open(); });
  search.addEventListener("input", open);
  search.addEventListener("blur", () => {
    close();
    // Restore the chosen zone, or accept an exact IANA id typed by hand.
    const typed = byId.get(search.value.trim());
    if (typed) choose(typed);
    else show(input.value);
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) return open();
      if (!matches.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
      highlight();
    } else if (e.key === "Enter" && !list.hidden) {
      e.preventDefault();
      if (matches[active]) choose(matches[active]);
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      show(input.value);
      close();
    }
  });

  return {
    value: () => input.value,
    set(id) { show(id); loaded.then(() => { if (input.value === id) show(id); }); },  // label once zones are in
    focus: () => search.focus(),
    ready: loaded,
  };
}
