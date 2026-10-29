// Views for the people whose runs the Steward reads: Students and Researchers see their
// own diagnoses, proposals and rebuilds; a supervising Professor reads their students'
// diagnoses (never proposals or the hardware finding, SADD §8.3).
import {
  api, h, clear, when, ago, projectName, minutes, badge, stateBadge, classificationBadge, cites,
  notices, sources, empty, loading, failure, pageHead, tabs, toast, confirm, CLASSIFICATION, VERDICT,
} from "./lib.js";

const isOwnerRole = (role) => role === "STUDENT" || role === "RESEARCHER";

// ── overview ────────────────────────────────────────────────────────────

export async function overview(ctx) {
  clear(ctx.view, pageHead("Overview", "What the Steward has found in your runs, and what is waiting for you."),
    loading());
  const [diagnoses, pending, rebuilds] = await Promise.all([
    api("/steward/diagnoses"), api("/steward/proposals?state=PROPOSED"), api("/rebuilds"),
  ]);
  const failed = rebuilds.filter((r) => r.state === "REBUILD_FAILED");
  const succeeded = rebuilds.filter((r) => r.state === "REBUILD_SUCCESS");
  clear(ctx.view,
    pageHead("Overview", "What the Steward has found in your runs, and what is waiting for you."),
    h("div", { class: "stats" },
      stat("Diagnosed failures", diagnoses.length, "#/diagnoses"),
      stat("Proposals to decide", pending.length, "#/proposals", pending.length ? "warn" : null),
      stat("Failed rebuilds", failed.length, "#/rebuilds", failed.length ? "bad" : null),
      stat("Rebuilds to optimise", succeeded.length, "#/rebuilds")),
    h("p", { class: "principle" },
      "The Steward reads and reasons; it never writes. Nothing changes until you approve it, and an approved fix is ",
      "queued like any new job: same quota, a new submit time, no priority advantage."),
    h("section", { class: "panel" },
      h("h2", null, "Waiting for your decision"),
      pending.length ? pending.slice(0, 3).map((p) => proposalCard(ctx, p, () => overview(ctx)))
        : empty("Nothing to decide", "When the Steward proposes a corrected job, it appears here.")),
    h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, "Recent diagnoses"),
        diagnoses.length ? h("a", { href: "#/diagnoses" }, "All diagnoses") : null),
      diagnoses.length ? diagnosisTable(diagnoses.slice(0, 5), false)
        : empty("No failed runs diagnosed yet", "When one of your runs fails, the Steward diagnoses it in the background.")));
}

function stat(label, value, href, tone) {
  return h("a", { class: `stat ${tone || ""}`, href },
    h("span", { class: "stat-value" }, String(value)), h("span", { class: "stat-label" }, label));
}

// ── S1 diagnoses ────────────────────────────────────────────────────────

export async function diagnosesList(ctx) {
  const professor = ctx.role === "PROFESSOR";
  const title = professor ? "Student diagnoses" : "Diagnoses";
  const sub = professor
    ? "Read-only diagnoses of failed runs by the students and researchers you supervise."
    : "Every failed run of yours that the Steward diagnosed, newest first.";
  clear(ctx.view, pageHead(title, sub), loading());
  const rows = await api("/steward/diagnoses");
  const lookup = h("form", {
    class: "inline-form", onsubmit: (e) => {
      e.preventDefault();
      const id = e.target.elements.run.value.trim();
      if (id) ctx.go(`#/runs/${encodeURIComponent(id)}`);
    },
  }, h("label", { for: "run-id" }, "Open a run by its ID"),
  h("input", { id: "run-id", name: "run", inputmode: "numeric", placeholder: "e.g. 42", autocomplete: "off" }),
  h("button", { type: "submit", class: "ghost" }, "Open"));
  clear(ctx.view, pageHead(title, sub, lookup),
    rows.length ? diagnosisTable(rows, professor)
      : empty("No diagnoses yet", professor
        ? "None of your students' failed runs has been diagnosed."
        : "When one of your runs fails, the Steward diagnoses it in the background. Nothing here blocks scheduling."));
}

function diagnosisTable(rows, showOwner) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", null, h("tr", null,
      h("th", null, "Run"), showOwner ? h("th", null, "Student") : null, h("th", null, "Project"),
      h("th", null, "Diagnosis"), showOwner ? null : h("th", null, "Proposal"), h("th", null, "When"))),
    h("tbody", null, rows.map((d) => h("tr", null,
      h("td", null, h("a", { href: `#/runs/${encodeURIComponent(d.run_id)}` }, `Run ${d.run_id}`)),
      showOwner ? h("td", null, d.mine ? "You" : `User ${d.owner_id}`) : null,
      h("td", null, projectName(d.project_id)),
      h("td", null, d.outcome === "ok" ? classificationBadge(d.classification)
        : badge("No diagnosis: model " + d.outcome, "warn")),
      showOwner ? null : h("td", null, d.proposal_state ? stateBadge(d.proposal_state) : h("span", { class: "muted" }, "–")),
      h("td", null, when(d.created_at)))))));
}

export async function runDetail(ctx, runId) {
  const back = h("a", { href: "#/diagnoses", class: "back" }, "← Diagnoses");
  clear(ctx.view, back, pageHead(`Run ${runId}`, "The Steward's diagnosis of this failed run."), loading());
  let list;
  try {
    list = await api(`/steward/runs/${encodeURIComponent(runId)}/diagnoses`);
  } catch (e) {
    if (e.status !== 404) throw e;
    clear(ctx.view, back, pageHead(`Run ${runId}`),
      empty("Run not found", "There is no run with this ID that you may see."));
    return;
  }
  const owner = isOwnerRole(ctx.role);
  const ownProposals = owner
    ? (await api("/steward/proposals")).filter((p) => p.run_id === String(runId)) : [];
  const refresh = h("button", { class: "ghost", type: "button", onclick: () => runDetail(ctx, runId) }, "Refresh");
  if (!list.length) {
    clear(ctx.view, back, pageHead(`Run ${runId}`, null, refresh),
      empty("Diagnosis pending", "The Steward diagnoses failed runs in the background; this run has no diagnosis yet. Runs that ended cancelled, out of walltime or interrupted get none."));
    return;
  }
  const [latest, ...older] = [...list].reverse();
  clear(ctx.view, back,
    pageHead(`Run ${runId}`, `Diagnosed ${ago(latest.created_at)} · ${latest.agent_version}`, refresh),
    diagnosisCard(latest, owner),
    ownProposals.length ? h("section", { class: "panel" }, h("h2", null, "Proposed fix"),
      ownProposals.map((p) => proposalCard(ctx, p, () => runDetail(ctx, runId)))) : null,
    older.length ? h("details", { class: "panel history" },
      h("summary", null, `Earlier diagnoses of this run (${older.length})`),
      older.map((d) => diagnosisCard(d, owner))) : null);
}

function diagnosisCard(d, owner) {
  const ok = d.outcome === "ok";
  return h("article", { class: "card diagnosis" },
    h("div", { class: "card-head" },
      ok ? h("span", { class: "label" }, "Diagnosis") : badge("Model " + d.outcome, "warn"),
      h("span", { class: "muted" }, when(d.created_at))),
    ok ? h("h2", { class: "diagnosis-title" }, CLASSIFICATION[d.classification] || "Unclassified failure") : null,
    ok ? null : h("p", { class: "notice" },
      "The Steward could not produce a diagnosis this time. Your run is sealed and unchanged; nothing was lost."),
    d.claims.length ? h("ul", { class: "claims" }, d.claims.map((c) =>
      h("li", { class: "claim" }, h("span", null, c.text), cites(c.cites)))) : null,
    d.stripped_count ? h("p", { class: "muted small" },
      `${d.stripped_count} statement${d.stripped_count === 1 ? " was" : "s were"} removed because ${d.stripped_count === 1 ? "it" : "they"} cited no evidence from this run.`) : null,
    d.matching_earlier_failures.length ? h("div", { class: "related" },
      h("span", { class: "label" }, "Failed the same way before"),
      d.matching_earlier_failures.map((ref) => {
        const id = ref.replace(/^earlier:/, "");
        return h("a", { class: "chip", href: `#/runs/${encodeURIComponent(id)}` }, `Run ${id}`);
      })) : null,
    owner && d.hardware_verdict ? hardwarePanel(d) : null);
}

function hardwarePanel(d) {
  const [label, tone] = VERDICT[d.hardware_verdict] || [d.hardware_verdict, "muted"];
  return h("div", { class: "hardware" },
    h("span", { class: "label" }, "Hardware"), badge(label, tone),
    d.hardware_indicators.map((i) => h("code", { class: "cite" }, i.replace(/_/g, " "))),
    h("span", { class: "muted small" },
      d.hardware_verdict === "likely_hardware"
        ? " Administrators see this finding as metadata only, never your diagnosis."
        : ""));
}

// ── proposals ───────────────────────────────────────────────────────────

const FIELD_LABEL = { gpu_count: "GPUs", walltime_minutes: "Walltime", command_args: "Command", image_digest: "Image" };
const formatField = (field, value) => (field === "walltime_minutes" ? minutes(value) : String(value ?? "–"));

export async function proposalsPage(ctx, state = "PROPOSED") {
  const pick = (s) => proposalsPage(ctx, s);
  const head = pageHead("Proposals", "Corrected jobs the Steward suggested. Only you can approve them.");
  const bar = tabs([["PROPOSED", "Awaiting decision"], ["APPROVED", "Approved"], ["REJECTED", "Rejected"]], state, pick);
  clear(ctx.view, head, bar, loading());
  const list = await api(`/steward/proposals?state=${state}`);
  clear(ctx.view, head, bar,
    list.length ? list.map((p) => proposalCard(ctx, p, () => proposalsPage(ctx, state)))
      : empty(state === "PROPOSED" ? "Nothing to decide" : `No ${state.toLowerCase()} proposals`,
        state === "PROPOSED" ? "A proposal appears when the Steward can fix a failed run by changing its GPUs or walltime." : null));
}

function changedFields(p) {
  const fields = new Set([...(p.changes || []).map((c) => c.field)]);
  for (const key of Object.keys(p.proposed_spec || {})) {
    if (p.current_spec && JSON.stringify(p.current_spec[key]) !== JSON.stringify(p.proposed_spec[key])) fields.add(key);
  }
  return [...fields];
}

export function proposalCard(ctx, p, refresh) {
  const fields = changedFields(p);
  const diff = h("table", { class: "diff" },
    h("thead", null, h("tr", null, h("th", null, "Setting"), h("th", null, "Failed run"), h("th", null, "Proposed"))),
    h("tbody", null, fields.map((f) => h("tr", null,
      h("th", { scope: "row" }, FIELD_LABEL[f] || f),
      h("td", { class: "old" }, p.current_spec ? formatField(f, p.current_spec[f]) : "–"),
      h("td", { class: "new" }, formatField(f, p.proposed_spec[f]))))));
  const gpuHours = Number(p.requested_gpu_hours);
  const decide = async (action) => {
    const approve = action === "approve";
    const ok = await confirm(
      approve ? "Queue the corrected job?" : "Reject this proposal?",
      approve
        ? [h("p", null, `This submits a new job for run ${p.run_id} with the settings below, requesting `,
          h("strong", null, `${gpuHours} GPU-hours`), "."), diff.cloneNode(true),
        h("p", { class: "muted small" }, "It goes through the normal submission path: same validation and quota, a new submit time and no priority advantage. The failed run itself is not changed.")]
        : h("p", null, "Nothing is queued. The proposal is kept as rejected."),
      approve ? "Approve and queue" : "Reject", approve ? "primary" : "danger");
    if (!ok) return;
    try {
      const result = await api(`/steward/proposals/${p.proposal_id}/${action}`, { method: "POST" });
      if (result.refusal) toast(`Not queued: ${result.refusal}`, "bad");
      else if (approve) toast(`Approved. Queued as job ${result.submitted_job_id}.`, "good");
      else toast("Proposal rejected. Nothing was queued.", "info");
    } catch (e) {
      toast(e.message, "bad");
    }
    refresh();
  };
  return h("article", { class: "card proposal" },
    h("div", { class: "card-head" }, stateBadge(p.state),
      h("a", { href: `#/runs/${encodeURIComponent(p.run_id)}` }, `Fix for run ${p.run_id}`),
      h("span", { class: "muted" }, when(p.created_at))),
    diff,
    (p.changes || []).length ? h("ul", { class: "claims" }, p.changes.map((c) => h("li", { class: "claim" },
      h("strong", null, (FIELD_LABEL[c.field] || c.field) + ": "), h("span", null, c.reason?.text || ""),
      cites(c.reason?.cites)))) : null,
    h("div", { class: "proposal-foot" },
      h("span", { class: "cost" }, h("span", { class: "label" }, "Requests"), h("strong", null, `${gpuHours} GPU-hours`)),
      p.submission_refusal ? h("span", { class: "refusal" }, "Last approval was not queued: " + p.submission_refusal) : null,
      p.state === "PROPOSED" && isOwnerRole(ctx.role) ? h("div", { class: "actions" },
        h("button", { class: "ghost", type: "button", onclick: () => decide("reject") }, "Reject"),
        h("button", { class: "primary", type: "button", onclick: () => decide("approve") }, "Approve…")) : null,
      p.state === "APPROVED" ? h("span", null, `Queued as job ${p.submitted_job_id}`, h("span", { class: "muted" }, " · ", when(p.decided_at))) : null,
      p.state === "REJECTED" ? h("span", { class: "muted" }, "Rejected ", when(p.decided_at)) : null));
}

// ── rebuilds: S3 links and S4 advice ────────────────────────────────────

export async function rebuildsPage(ctx) {
  const head = pageHead("Rebuilds", "Re-running a recorded snapshot. A failed rebuild can be diagnosed with the Steward; a successful one can get optimisation advice.");
  clear(ctx.view, head, loading());
  const list = await api("/rebuilds");
  clear(ctx.view, head, startRebuildForm(ctx),
    list.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", null, "Attempt"), h("th", null, "Snapshot"), h("th", null, "Job"),
        h("th", null, "State"), h("th", null, "Started"), h("th", null, h("span", { class: "visually-hidden" }, "Actions")))),
      h("tbody", null, list.map((r) => h("tr", null,
        h("td", null, `#${r.attempt_id}`),
        h("td", null, h("code", { title: r.snapshot_hash }, short(r.snapshot_hash))),
        h("td", null, `Job ${r.job_id}`),
        h("td", null, stateBadge(r.state)),
        h("td", null, when(r.created_at)),
        h("td", { class: "row-actions" }, rebuildAction(ctx, r)))))))
      : empty("No rebuilds yet", "Rebuild a snapshot with the CLI (prayog run --rebuild-of A/1) or the form above."));
}

const short = (hash) => (hash && hash.length > 16 ? hash.slice(0, 12) + "…" : hash || "–");

function rebuildAction(ctx, r) {
  if (r.state === "REBUILD_FAILED") {
    return h("button", { class: "primary small", type: "button", onclick: (e) => openSession(ctx, r.attempt_id, e.target) },
      "Diagnose with Steward");
  }
  if (r.state === "REBUILD_SUCCESS") {
    return h("a", { class: "button ghost small", href: `#/rebuilds/${r.attempt_id}/advice` }, "Optimisation advice");
  }
  return null;
}

// The owner asks for a single-use link and opens it straight away. The link is bound to
// this sign-in and expires in 15 minutes; opening it starts the one-hour chat session.
async function openSession(ctx, attemptId, button) {
  button.disabled = true;
  try {
    const link = await api(`/steward/attempts/${attemptId}/link`, { method: "POST" });
    const tokenPath = new URL(link.url, location.origin).pathname;
    await api(`${tokenPath}/open`, { method: "POST" });
    ctx.go("#/chat");
  } catch (e) {
    toast(e.message, "bad");
    button.disabled = false;
  }
}

function startRebuildForm(ctx) {
  const status = h("p", { class: "status", role: "status" });
  const form = h("form", {
    class: "grid-form", onsubmit: async (e) => {
      e.preventDefault();
      const f = e.target.elements;
      status.textContent = "Submitting…";
      try {
        const attempt = await api("/rebuilds", {
          method: "POST",
          body: {
            snapshot_hash: f.snapshot.value.trim(), gpus: Number(f.gpus.value), walltime: f.walltime.value,
            client_request_id: crypto.randomUUID(),
          },
        });
        toast(`Rebuild attempt #${attempt.attempt_id} queued as job ${attempt.job_id}.`, "good");
        rebuildsPage(ctx);
      } catch (err) {
        status.textContent = err.message;
      }
    },
  },
  field("Snapshot ID", h("input", { name: "snapshot", required: true, autocomplete: "off", spellcheck: "false" })),
  field("GPUs", h("input", { name: "gpus", type: "number", min: "1", max: "64", value: "1", required: true })),
  field("Walltime (hh:mm)", h("input", { name: "walltime", pattern: "\\d{1,3}:\\d{2}", value: "00:30", required: true })),
  h("button", { type: "submit", class: "primary" }, "Start rebuild"), status);
  return h("details", { class: "panel collapsible" }, h("summary", null, "Start a rebuild"), form);
}

function field(label, input) {
  return h("label", { class: "field" }, h("span", null, label), input);
}

export async function advicePage(ctx, attemptId) {
  const back = h("a", { href: "#/rebuilds", class: "back" }, "← Rebuilds");
  const head = pageHead(`Optimisation advice · attempt #${attemptId}`,
    "Suggestions for this successful rebuild, drawn from your own runs and reference papers. Advisory only: nothing is changed or run.");
  const out = h("div", { class: "stack" });
  const ask = h("button", { class: "primary", type: "button" }, "Ask the Steward");
  ask.addEventListener("click", async () => {
    ask.disabled = true;
    clear(out, loading("The Steward is reading your run and the reference papers…"));
    try {
      const advice = await api(`/steward/attempts/${encodeURIComponent(attemptId)}/optimize`, { method: "POST" });
      clear(out, renderAdvice(advice));
    } catch (e) {
      clear(out, failure(e));
    }
    ask.disabled = false;
    ask.textContent = "Ask again";
  });
  clear(ctx.view, back, head, h("div", { class: "actions" }, ask), out);
}

const KIND_LABEL = { architecture: "Architecture", hyperparameter: "Hyperparameters", training_setup: "Training setup" };

function renderAdvice(advice) {
  const groups = {};
  for (const s of advice.suggestions || []) (groups[s.kind] ||= []).push(s);
  return [notices(advice.notices),
    Object.keys(groups).length ? Object.entries(groups).map(([kind, items]) => h("section", { class: "card" },
      h("h2", null, KIND_LABEL[kind] || kind),
      h("ul", { class: "claims" }, items.map((s) => h("li", { class: "claim" }, h("span", null, s.text), cites(s.cites))))))
      : empty("No suggestions", advice.outcome === "ok" ? "The Steward found nothing to suggest for this run." : null),
    sources(advice.sources)];
}

// ── reference papers ────────────────────────────────────────────────────

export async function knowledgePage(ctx) {
  const head = pageHead("Reference papers", "The literature the Steward may cite in optimisation advice and rebuild diagnoses. Read-only.");
  clear(ctx.view, head, loading());
  const docs = await api("/steward/knowledge/documents");
  clear(ctx.view, head, docs.length
    ? h("div", { class: "cards" }, docs.map((d) => h("article", { class: "card paper" },
      h("h2", null, d.url && /^https?:\/\//.test(d.url)
        ? h("a", { href: d.url, rel: "noopener noreferrer", target: "_blank" }, d.title || d.source) : d.title || d.source),
      h("p", { class: "muted" }, [d.authors, d.published].filter(Boolean).join(" · ")),
      h("p", { class: "small muted" }, `${d.chunks} indexed passages · `, h("code", null, d.source)))))
    : empty("No reference papers loaded", "An administrator adds papers with the knowledge ingest tool; see steward/DESIGN.md."));
}
