// Steward S3 web UI. All model output is rendered with textContent, never innerHTML,
// so nothing the model or a log says can inject markup or script (FR-STW-016).
"use strict";

const API = "/steward";
const $ = (id) => document.getElementById(id);
const thread = $("thread");
const statusEl = $("status");

const PROVENANCE = {
  internal: ["Your records", "badge internal"],
  external: ["Web · not guaranteed", "badge external"],
  literature: ["Literature", "badge literature"],
  reasoning: ["General reasoning", "badge reasoning"],
};

async function call(path, options = {}) {
  const response = await fetch(API + path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-Steward-CSRF": "1", ...(options.headers || {}) },
  });
  let body = null;
  try { body = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    const detail = (body && body.detail) || "Something went wrong.";
    throw new Error(detail);
  }
  return body;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderTurn(question, answer) {
  const node = $("tpl-turn").content.firstElementChild.cloneNode(true);
  node.querySelector(".q").textContent = question;
  const notices = node.querySelector(".notices");
  for (const n of answer.notices || []) notices.appendChild(el("li", "notice", n));

  const claims = node.querySelector(".claims");
  for (const c of answer.claims || []) {
    const li = el("li", "claim");
    const [label, cls] = PROVENANCE[c.provenance] || ["", "badge"];
    li.appendChild(el("span", cls, label));
    li.appendChild(el("span", "text", " " + c.text));
    if (c.cites && c.cites.length) li.appendChild(el("span", "cites", " [" + c.cites.join(", ") + "]"));
    claims.appendChild(li);
  }

  const steps = node.querySelector(".steps");
  for (const s of answer.steps || []) {
    const li = el("li", "step", s.explanation);
    if (s.command) {
      const box = el("div", "cmd");
      box.appendChild(el("pre", null, s.command));
      const copy = el("button", "ghost small", "Copy");
      copy.type = "button";
      copy.addEventListener("click", () => navigator.clipboard.writeText(s.command));
      box.appendChild(copy);
      li.appendChild(box);
    }
    steps.appendChild(li);
  }

  const sources = node.querySelector(".sources");
  const entries = Object.entries(answer.sources || {});
  if (entries.length) {
    sources.appendChild(el("span", "muted", "Sources: "));
    entries.forEach(([id, src], i) => {
      if (i) sources.appendChild(document.createTextNode(" · "));
      if (src.url && /^https?:\/\//.test(src.url)) {
        const a = el("a", null, src.title || id);
        a.title = id;
        a.href = src.url; a.rel = "noopener noreferrer nofollow"; a.target = "_blank";
        sources.appendChild(a);
      } else {
        sources.appendChild(el("code", null, id));
      }
    });
  }
  thread.appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
}

async function load() {
  try {
    const s = await call("/s3/session", { method: "GET" });
    $("attempt").textContent = s.attempt_id;
    $("expiry").textContent = "Session ends " + new Date(s.expires_at).toLocaleTimeString();
    let pending = null;
    for (const m of s.history) {
      if (m.role === "user") pending = m.content.question;
      else if (pending !== null) { renderTurn(pending, m.content); pending = null; }
    }
  } catch (e) {
    $("ask").hidden = true;
    statusEl.textContent = e.message + " Open a new Steward link from the CLI to continue.";
  }
}

$("ask").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const include = $("include").value.split(",").map((x) => x.trim()).filter(Boolean);
  $("send").disabled = true;
  statusEl.textContent = "Steward is reading your records…";
  try {
    const answer = await call("/s3/messages", {
      method: "POST",
      body: JSON.stringify({ question, include_attempts: include }),
    });
    renderTurn(question, answer);
    $("question").value = "";
    statusEl.textContent = answer.cached ? "Answered from this session's cache." : "";
  } catch (e) {
    statusEl.textContent = e.message;
  } finally {
    $("send").disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try { await call("/s3/logout", { method: "POST" }); } catch (_) { /* already gone */ }
  $("ask").hidden = true;
  statusEl.textContent = "Session ended. Your rebuild attempt is unchanged.";
});

load();
