// Platform Admin views: S2 hardware warnings and Steward health. Metadata only: no logs,
// no diagnoses and no model text from users ever reach these screens (NFR-AUTH-18,
// FR-GPU-158). Decisions go through the platform's own resolve route, which freezes the
// unit or drains the node and then closes the warning; a warning alone never changes
// node state.
import {
  api, h, clear, when, badge, stateBadge, cites, empty, loading, pageHead, tabs, toast, confirm, VERDICT,
} from "./lib.js";

export async function warningsPage(ctx, state = "OPEN") {
  const head = pageHead("Hardware warnings",
    "Raised by the server when failures from two or more labs land on one GPU within the window. The Steward's finding is advisory.");
  const bar = tabs([["OPEN", "Open"], ["RESOLVED", "Resolved"], ["DISMISSED", "Dismissed"]], state, (s) => warningsPage(ctx, s));
  clear(ctx.view, head, bar, loading());
  const list = await api(`/steward/admin/warnings?state=${state}`);
  clear(ctx.view, head, bar, list.length
    ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", null, "GPU"), h("th", null, "Labs"), h("th", null, "Failures"),
        h("th", null, "Steward finding"), h("th", null, state === "OPEN" ? "Opened" : "Closed"))),
      h("tbody", null, list.map((w) => h("tr", null,
        h("td", null, h("a", { href: `#/admin/warnings/${w.warning_id}` }, w.unit_signature)),
        h("td", null, String(w.distinct_labs)),
        h("td", null, String(w.evidence_count)),
        h("td", null, findingBadge(w.steward_finding)),
        h("td", null, when(state === "OPEN" ? w.opened_at : w.closed_at)))))))
    : empty(state === "OPEN" ? "No open warnings" : `No ${state.toLowerCase()} warnings`,
      state === "OPEN" ? "All GPUs look healthy from the failure evidence." : null));
}

function findingBadge(finding) {
  if (!finding) return h("span", { class: "muted" }, "Pending");
  const verdict = finding.finding && finding.finding.verdict;
  const [label, tone] = VERDICT[verdict] || [finding.same_fault ? "Same fault" : "Not the same fault", finding.same_fault ? "bad" : "muted"];
  return badge(label, tone);
}

export async function warningDetail(ctx, warningId) {
  const back = h("a", { href: "#/admin/warnings", class: "back" }, "← Hardware warnings");
  clear(ctx.view, back, loading());
  // The platform's investigation view adds the unit's current state; fall back to the
  // Steward's own view when the scheduler side cannot answer.
  let warning, unit = null, platform = true;
  try {
    ({ warning, unit } = await api(`/admin/hardware-warnings/${warningId}`));
  } catch (e) {
    if (e.status === 404 && e.body.detail !== "Not Found") throw e;
    platform = false;
    warning = await api(`/steward/admin/warnings/${warningId}`);
  }
  const f = warning.steward_finding;
  clear(ctx.view, back,
    pageHead(warning.unit_signature, `Node ${warning.node_id} · opened ${new Date(warning.opened_at).toLocaleString()}`,
      stateBadge(warning.state)),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("span", { class: "stat-value" }, String(warning.distinct_labs)), h("span", { class: "stat-label" }, "Labs affected")),
      h("div", { class: "stat" }, h("span", { class: "stat-value" }, String(warning.evidence_count)), h("span", { class: "stat-label" }, "Failures cited")),
      unit ? h("div", { class: "stat" }, h("span", { class: "stat-value small-value" }, unit.node_state.toLowerCase()),
        h("span", { class: "stat-label" }, `Node ${unit.node_name}${unit.allocated ? " · unit busy" : " · unit idle"}`)) : null,
      unit && unit.maintenance_until ? h("div", { class: "stat" }, h("span", { class: "stat-value small-value" }, "frozen"),
        h("span", { class: "stat-label" }, "until ", when(unit.maintenance_until))) : null),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, "Steward finding"), badge("Advisory", "muted")),
      f ? [
        h("p", null, findingBadge(f), " ",
          f.same_fault ? "The failures look like one fault on this unit." : "The failures do not look like one fault."),
        f.finding && f.finding.indicators && f.finding.indicators.length
          ? h("p", null, f.finding.indicators.map((i) => h("code", { class: "cite" }, i.replace(/_/g, " ")))) : null,
        (f.rationale || []).length ? h("ul", { class: "claims" }, f.rationale.map((r) =>
          h("li", { class: "claim" }, h("span", null, r.text), cites(r.cites)))) : null,
      ] : h("p", { class: "muted" }, "The Steward has not looked at this warning yet. The warning stands on the server's threshold either way.")),
    warning.state === "OPEN" ? decisionForm(ctx, warning, platform && unit) : closedNote(warning),
    h("section", { class: "panel" }, h("h2", null, "Evidence"),
      (warning.evidence || []).length ? evidenceTable(warning.evidence) : empty("No evidence rows", null)),
    (warning.allocation_history || []).length ? h("section", { class: "panel" },
      h("h2", null, "Recent use of these GPUs"), genericTable(warning.allocation_history)) : null);
}

function closedNote(w) {
  return h("section", { class: "card" }, h("h2", null, w.state === "RESOLVED" ? "Resolved" : "Dismissed"),
    h("p", null, w.close_reason || ""),
    h("p", { class: "muted small" }, "Closed ", when(w.closed_at), w.closed_by ? ` by admin ${w.closed_by}` : ""));
}

function evidenceTable(rows) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
    h("thead", null, h("tr", null, ["Run", "Job", "Lab", "GPU", "When", "Exit", "Category", "Verdict"].map((c) => h("th", null, c)))),
    h("tbody", null, rows.map((e) => h("tr", null,
      h("td", null, e.run_id), h("td", null, e.job_id), h("td", null, e.owner_lab), h("td", null, e.unit_id),
      h("td", null, when(e.at)), h("td", null, e.exit_code ?? "–"), h("td", null, e.failure_category || "–"),
      h("td", null, e.hardware_verdict ? badge((VERDICT[e.hardware_verdict] || [e.hardware_verdict])[0],
        (VERDICT[e.hardware_verdict] || [0, "muted"])[1]) : "–"))))));
}

function genericTable(rows) {
  const cols = Object.keys(rows[0]);
  const show = (v) => (v === null || v === undefined ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v));
  return h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
    h("thead", null, h("tr", null, cols.map((c) => h("th", null, c.replace(/_/g, " "))))),
    h("tbody", null, rows.map((r) => h("tr", null, cols.map((c) => h("td", null,
      /_at$|^at$/.test(c) && r[c] ? when(r[c]) : show(r[c]))))))));
}

function decisionForm(ctx, warning, platformUnit) {
  const status = h("p", { class: "status", role: "status" });
  const tomorrow = new Date(Date.now() + 86400000);
  const local = new Date(tomorrow.getTime() - tomorrow.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const actions = platformUnit
    ? [["freeze", "Freeze this GPU", "Book it out of scheduling until a time you choose."],
      ["drain", "Drain the node", "No new work on the node; running jobs finish (soft) or stop (hard)."],
      ["dismiss", "Dismiss", "The evidence does not point at hardware."]]
    : [["resolve", "Mark resolved", "You froze or drained it elsewhere."], ["dismiss", "Dismiss", "The evidence does not point at hardware."]];
  const form = h("form", { class: "decision" },
    h("fieldset", null, h("legend", null, "Decision"),
      actions.map(([value, label, help], i) => h("label", { class: "radio" },
        h("input", { type: "radio", name: "action", value, checked: i === 0 }),
        h("span", null, h("strong", null, label), h("span", { class: "muted small" }, " " + help))))),
    platformUnit ? h("div", { class: "row" },
      h("label", { class: "field", "data-for": "freeze" }, h("span", null, "Frozen until"),
        h("input", { type: "datetime-local", name: "until", value: local })),
      h("label", { class: "field", "data-for": "drain" }, h("span", null, "Drain mode"),
        h("select", { name: "mode" }, h("option", { value: "soft" }, "Soft: let running jobs finish"),
          h("option", { value: "hard" }, "Hard: stop running jobs")))) : null,
    h("label", { class: "field" }, h("span", null, "Reason (recorded with the decision)"),
      h("textarea", { name: "reason", rows: "2", required: true, maxlength: "1000", placeholder: "e.g. Froze gpu0; vendor ticket 4411" })),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Record decision")), status);

  const sync = () => {
    const action = form.elements.action.value;
    form.querySelectorAll("[data-for]").forEach((n) => { n.hidden = n.dataset.for !== action; });
  };
  form.addEventListener("change", sync);
  sync();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const action = f.action.value;
    const reason = f.reason.value.trim();
    if (!reason) { status.textContent = "A reason is required."; return; }
    status.textContent = "Recording…";
    try {
      if (!platformUnit) {
        await api(`/steward/admin/warnings/${warning.warning_id}/close`, {
          method: "POST", body: { resolution: action === "dismiss" ? "DISMISSED" : "RESOLVED", reason },
        });
      } else {
        const body = { action, reason, drain_mode: f.mode.value };
        if (action === "freeze") {
          if (!f.until.value) { status.textContent = "Choose until when the GPU stays frozen."; return; }
          body.freeze_until = new Date(f.until.value).toISOString();
        }
        await resolve(warning.warning_id, body);
      }
      toast("Decision recorded. The warning is closed and its evidence consumed.", "good");
      warningDetail(ctx, warning.warning_id);
    } catch (err) {
      status.textContent = err.cancelled ? "" : err.message;
    }
  });
  return h("section", { class: "card" }, h("h2", null, "Close this warning"), form);
}

// A freeze that would stop running jobs is refused with the jobs at risk; show them and
// resend with confirm only if the admin agrees (FR-GPU-084).
async function resolve(warningId, body) {
  try {
    return await api(`/admin/hardware-warnings/${warningId}/resolve`, { method: "POST", body });
  } catch (e) {
    if (e.status !== 409 || !Array.isArray(e.body.collisions)) throw e;
    const ok = await confirm("Running jobs would be stopped", [
      h("p", null, e.message),
      h("ul", null, e.body.collisions.map((c) => h("li", null,
        `Job ${c.job_id} (${c.owner}) · ${c.gpu_hours_at_risk} GPU-hours at risk`))),
    ], "Freeze anyway", "danger");
    if (!ok) throw Object.assign(new Error("cancelled"), { cancelled: true });
    return api(`/admin/hardware-warnings/${warningId}/resolve`, { method: "POST", body: { ...body, confirm: true } });
  }
}

export async function healthPage(ctx) {
  const head = pageHead("Steward health", "Aggregates only: no user content appears here.");
  clear(ctx.view, head, loading());
  const [health, docs] = await Promise.all([api("/steward/admin/health"), api("/steward/knowledge/documents").catch(() => [])]);
  const runtime = health.runtime_reachable;
  const queue = Object.entries(health.task_queue || {});
  clear(ctx.view, head,
    h("div", { class: "stats" },
      h("div", { class: `stat ${runtime === false ? "bad" : ""}` },
        h("span", { class: "stat-value small-value" }, runtime === true ? "Reachable" : runtime === false ? "Down" : "In-process"),
        h("span", { class: "stat-label" }, "Model runtime")),
      h("div", { class: "stat" }, h("span", { class: "stat-value" }, String(queue.reduce((n, [, v]) => n + v, 0))),
        h("span", { class: "stat-label" }, "Steward tasks")),
      h("div", { class: "stat" }, h("span", { class: "stat-value" }, String(docs.length)),
        h("span", { class: "stat-label" }, "Reference papers")),
      health.rag ? h("div", { class: "stat" }, h("span", { class: "stat-value" }, String(health.rag.sessions)),
        h("span", { class: "stat-label" }, "Chat sessions on record")) : null),
    runtime === false ? h("p", { class: "notice" },
      "The model runtime is down. Runs still seal and scheduling is unaffected; Steward tasks wait and retry.") : null,
    h("section", { class: "card" }, h("h2", null, "Task queue"),
      queue.length ? h("dl", { class: "kv" }, queue.flatMap(([k, v]) => [h("dt", null, k.toLowerCase()), h("dd", null, String(v))]))
        : h("p", { class: "muted" }, "No tasks yet.")),
    h("section", { class: "card" }, h("h2", null, "Retrieval"),
      h("dl", { class: "kv" },
        h("dt", null, "Embedder"), h("dd", null, h("code", null, health.embedder)),
        h("dt", null, "Web search"), h("dd", null, health.search_provider === "none" ? "Disabled" : health.search_provider),
        h("dt", null, "Knowledge index"), h("dd", null, health.knowledge
          ? `${health.knowledge.chunks} passages · ${health.knowledge.embedder}` : "Not loaded"),
        health.rag ? [h("dt", null, "Private RAG store"),
          h("dd", null, `${health.rag.chunks} chunks · ${(health.rag.bytes / 1024).toFixed(1)} KiB`)] : null)));
}
