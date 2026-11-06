// S3: the interactive diagnosis of a failed rebuild. The session is an HttpOnly cookie the
// server set when the owner opened a single-use link; this view only reads and asks.
import { api, h, clear, when, cites, notices, sources, empty, loading, pageHead, toast } from "./lib.js";

const PROVENANCE = {
  internal: ["Your records", "internal"],
  literature: ["Literature", "literature"],
  external: ["Web · not guaranteed", "external"],
  reasoning: ["General reasoning", "reasoning"],
};

export async function chatPage(ctx) {
  clear(ctx.view, pageHead("Steward chat", "Ask why a rebuild failed."), loading());
  let session;
  try {
    session = await api("/steward/s3/session");
  } catch (e) {
    if (e.status !== 403 && e.status !== 404) throw e;
    clear(ctx.view, pageHead("Steward chat", "Ask why a rebuild failed."),
      empty("No open Steward session",
        "A session starts from a failed rebuild and lasts an hour. Open one from Rebuilds, or from the link the CLI printed.",
        h("a", { class: "button primary", href: "#/rebuilds" }, "Go to rebuilds")));
    return;
  }
  const attempts = await api("/rebuilds").catch(() => []);
  const thread = h("section", { class: "thread", "aria-live": "polite" });
  const status = h("p", { class: "status", role: "status" });

  let pending = null;
  for (const m of session.history || []) {
    if (m.role === "user") pending = m.content.question;
    else if (pending !== null) { thread.appendChild(turn(pending, m.content)); pending = null; }
  }
  if (!thread.childElementCount) {
    thread.appendChild(h("div", { class: "starter" },
      h("p", null, "Answers draw only on your own records for this attempt, the reference papers, and the web if your administrator enabled search. Each statement says where it came from."),
      h("div", { class: "suggestions" }, [
        "Why did this rebuild fail?",
        "What changed since the run that worked?",
        "How do I fix it?",
      ].map((q) => h("button", { type: "button", class: "chip", onclick: () => send(q) }, q)))));
  }

  const question = h("textarea", {
    id: "question", rows: "2", maxlength: "2000", required: true,
    placeholder: "Ask about this failed rebuild…",
  });
  question.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  const others = attempts.filter((a) => String(a.attempt_id) !== String(session.attempt_id)).slice(0, 20);
  const include = others.length ? h("details", { class: "include" },
    h("summary", null, "Compare with earlier attempts"),
    h("p", { class: "muted small" }, "Up to five of your own attempts can be brought into context."),
    h("div", { class: "checks" }, others.map((a) => h("label", { class: "check" },
      h("input", { type: "checkbox", name: "include", value: String(a.attempt_id) }),
      ` #${a.attempt_id} · ${a.state.replace("REBUILD_", "").toLowerCase()}`)))) : null;
  const sendButton = h("button", { type: "submit", class: "primary" }, "Ask");
  const form = h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); send(question.value); } },
    h("label", { for: "question", class: "visually-hidden" }, "Your question"), question,
    h("div", { class: "composer-row" }, include, sendButton));

  async function send(text) {
    const q = text.trim();
    if (!q) return;
    const picked = [...form.querySelectorAll("input[name=include]:checked")].map((i) => i.value);
    if (picked.length > 5) { status.textContent = "Choose at most five earlier attempts."; return; }
    sendButton.disabled = true;
    thread.querySelector(".starter")?.remove();
    status.textContent = "The Steward is reading your records…";
    try {
      const answer = await api("/steward/s3/messages", { method: "POST", body: { question: q, include_attempts: picked } });
      thread.appendChild(turn(q, answer));
      thread.lastElementChild.scrollIntoView({ behavior: "smooth", block: "start" });
      question.value = "";
      status.textContent = answer.cached ? "Answered from this session's cache." : "";
    } catch (e) {
      status.textContent = e.status === 403 ? "This session has ended. Open a new one from Rebuilds." : e.message;
    } finally {
      sendButton.disabled = false;
    }
  }

  const end = h("button", {
    type: "button", class: "ghost", onclick: async () => {
      try { await api("/steward/s3/logout", { method: "POST" }); } catch (_) { /* already gone */ }
      toast("Session ended. Your rebuild attempt is unchanged.");
      chatPage(ctx);
    },
  }, "End session");
  clear(ctx.view,
    pageHead(`Rebuild #${session.attempt_id}`, null,
      h("span", { class: "muted" }, "Session ends ", when(session.expires_at)), end),
    h("p", { class: "principle" },
      "Suggested commands are text for you to run yourself. The Steward never runs anything or changes your files."),
    thread, status, form);
  question.focus();
}

function turn(question, answer) {
  return h("article", { class: "turn" },
    h("p", { class: "q" }, question),
    h("div", { class: "a" },
      notices(answer.notices),
      answer.outcome && answer.outcome !== "ok"
        ? h("p", { class: "notice" }, "The Steward could not answer just now. Your rebuild stays REBUILD_FAILED; try again shortly.") : null,
      answer.evidence_found === false && answer.outcome === "ok"
        ? h("p", { class: "muted" }, "No evidence was found in your records for this question.") : null,
      (answer.claims || []).length ? h("ul", { class: "claims" }, answer.claims.map((c) => {
        const [label, cls] = PROVENANCE[c.provenance] || [c.provenance, "reasoning"];
        return h("li", { class: "claim" }, h("span", { class: `prov ${cls}` }, label), h("span", null, c.text), cites(c.cites));
      })) : null,
      (answer.steps || []).length ? h("div", { class: "steps" }, h("span", { class: "label" }, "Steps to try"),
        h("ol", null, answer.steps.map((s) => h("li", null, h("p", null, s.explanation), s.command ? command(s.command) : null)))) : null,
      answer.search === "unavailable" ? h("p", { class: "muted small" }, "Web search was unavailable; this answer uses your records only.") : null,
      sources(answer.sources)));
}

function command(text) {
  return h("div", { class: "cmd" }, h("pre", null, h("code", null, text)),
    h("button", {
      type: "button", class: "ghost small", onclick: async (e) => {
        try { await navigator.clipboard.writeText(text); e.target.textContent = "Copied"; } catch (_) { e.target.textContent = "Copy failed"; }
      },
    }, "Copy"));
}
