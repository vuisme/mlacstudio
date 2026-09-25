"use strict";

(async () => {
  const form = document.querySelector("form");
  const error = document.getElementById("error");
  const button = form.querySelector("button");
  const status = await fetch("/api/auth/status").then((res) => res.json());
  if (status.authenticated) { location.replace("/"); return; }
  const setup = form.dataset.mode === "setup";
  if (setup !== !status.configured) { location.replace(status.configured ? "/login" : "/setup"); return; }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    const body = Object.fromEntries(new FormData(form).entries());
    try {
      const response = await fetch(setup ? "/api/setup" : "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": status.csrf },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      location.replace("/");
    } catch (caught) {
      error.textContent = caught.message;
      error.hidden = false;
      button.disabled = false;
    }
  });
})().catch((error) => {
  const node = document.getElementById("error");
  node.textContent = error.message;
  node.hidden = false;
});
