// Vapi phone number / assistant dropdowns: value = id, label "Name (abcd...)".

import { vapiLabel } from "./format.js";
import { onLists } from "./store.js";

const selects = document.querySelectorAll("select[data-vapi]");

// Fill every dropdown from the Vapi lists, or show a message in all of them (vapi = null).
function setOptions(message, vapi) {
  for (const select of selects) {
    const previous = select.value;
    select.replaceChildren(new Option(vapi ? select.dataset.empty : message, ""));
    if (vapi) {
      for (const item of vapi[select.dataset.vapi]) select.add(new Option(vapiLabel(item), item.id));
      select.value = previous;
      if (select.value !== previous) select.value = "";
    }
    select.disabled = !vapi;
  }
}

onLists((event) => {
  if (event.list !== "vapi") return;
  if (event.state === "loading") setOptions("Loading…");
  else if (event.state === "signedOut") setOptions("Sign in to load");
  else if (event.state === "error") setOptions("Could not load from Vapi");
  else setOptions(null, event.data);
});
