"use strict";
const out = document.getElementById("out");
const show = (x) => { out.textContent = typeof x === "string" ? x : JSON.stringify(x, null, 2); };
let lastAttempt = null;

async function call(method, path) {
  const r = await fetch(path, { method, credentials: "same-origin",
                                headers: { "X-Steward-CSRF": "1" } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${r.status}: ${body.detail || "error"}`);
  return body;
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.post) {
      const body = await call("POST", b.dataset.post);
      show(body);
      if (body.cli_output) {
        lastAttempt = body.successful_attempt;
        const p = document.getElementById("link");
        p.textContent = "CLI would print: ";
        const a = document.createElement("a");
        a.href = body.cli_output; a.textContent = body.cli_output;
        p.appendChild(a);
      }
      setTimeout(() => {}, 0);
    } else if (b.dataset.get) {
      show(await call("GET", b.dataset.get));
    } else if ("approve" in b.dataset) {
      const pending = await call("GET", "/steward/proposals?state=PROPOSED");
      if (!pending.length) return show("No pending proposals (wait a second for the worker).");
      show(await call("POST", `/steward/proposals/${pending[0].proposal_id}/approve`));
    } else if ("optimize" in b.dataset) {
      if (!lastAttempt) return show("Fail a rebuild first; it also creates a successful one.");
      show(await call("POST", `/steward/attempts/${lastAttempt}/optimize`));
    }
  } catch (err) { show(err.message); }
});
