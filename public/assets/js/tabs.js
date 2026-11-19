// Top menu: switches between the Customers and Debug views.

const buttons = document.querySelectorAll(".tabs [data-view]");

export function showView(id) {
  for (const button of buttons) {
    const active = button.dataset.view === id;
    document.getElementById(button.dataset.view).hidden = !active;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

for (const button of buttons) button.addEventListener("click", () => showView(button.dataset.view));
