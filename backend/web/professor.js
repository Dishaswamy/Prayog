// The Professor workspace: read-only views of supervised users' ledgers (FR-ML-004) and the
// review of IP handovers (FR-ML-009). No infrastructure operation appears here (FR-INT-05).
import { api, h, clear, when, empty, loading, pageHead, tabs, toast, confirm } from "/app/steward/lib.js";
import { bytes, json, kv, metricCharts, stamp, status, table, words } from "./ui.js";

export async function workspacePage(ctx) {
  const head = pageHead("Students", "Projects and recorded runs of the people you supervise. Everything here is read-only.");
  clear(ctx.view, head, loading());
  const [projects, ledger] = await Promise.all([api("/mlflow/professor/projects"), api("/mlflow/professor/ledger")]);
  clear(ctx.view, head,
    h("section", { class: "panel" }, h("h2", null, "Projects"),
      projects.length ? table(["Project", "Description", "Updated"], projects.map((p) => [
        h("a", { href: `#/workspace/projects/${p.project_id}` }, p.name), p.description || "–", when(p.updated_at),
      ])) : empty("No projects yet", "Projects appear here once someone you supervise creates one.")),
    h("section", { class: "panel" }, h("h2", null, "Recorded snapshots"),
      ledger.length ? table(["Student", "Roll number", "Run", "Sealed", "State"], ledger.map((e) => [
        e.student_name, e.student_roll_number, h("a", { href: `#/workspace/runs/${e.run_id}` }, `Run ${e.run_id}`), when(e.sealed_at), status(e.state),
      ])) : empty("No recorded snapshots", "Sealed snapshots appear here once the ledger consumer has recorded them.")));
}

export async function studentProject(ctx, pid) {
  const back = h("a", { href: "#/workspace", class: "back" }, "← Students");
  clear(ctx.view, back, loading());
  const projects = await api("/mlflow/professor/projects");
  const project = projects.find((p) => p.project_id === pid);
  const experiments = await api(`/mlflow/professor/projects/${pid}/experiments`);
  const runs = await Promise.all(experiments.map((x) => api(`/mlflow/professor/experiments/${x.id}/runs`)));
  clear(ctx.view, back, pageHead(project ? project.name : "Project", project?.description || "Read-only."),
    experiments.length ? experiments.map((x, i) => h("section", { class: "panel" }, h("h2", null, x.name),
      runs[i].length ? table(["Run", "Type", "Status", "Created"], runs[i].map((r) => [
        h("a", { href: `#/workspace/runs/${r.id}` }, `Run ${r.id}`), r.run_type === "REBUILD" ? "Rebuild" : "First run", status(r.status), when(r.created_at),
      ])) : empty("No runs"))) : empty("No experiments yet"));
}

export async function studentRun(ctx, id) {
  const back = h("a", { href: "#/workspace", class: "back" }, "← Students");
  clear(ctx.view, back, loading());
  const [run, metrics] = await Promise.all([api(`/mlflow/professor/runs/${id}`), api(`/mlflow/professor/runs/${id}/metrics`)]);
  clear(ctx.view,
    h("a", { href: `#/workspace/projects/${run.project_id}`, class: "back" }, "← Project"),
    pageHead(`Run ${run.id}`, "Read-only.", status(run.status)),
    run.error_summary ? h("p", { class: "notice" }, run.error_summary) : null,
    h("section", { class: "card" }, kv([
      ["Type", run.run_type === "REBUILD" ? "Rebuild" : "First run"], ["Recording", words(run.logging_state)],
      ["Created", stamp(run.created_at)], run.started_at ? ["Started", stamp(run.started_at)] : null,
      run.finished_at ? ["Finished", stamp(run.finished_at)] : null,
    ])),
    h("section", { class: "panel" }, h("h2", null, "Metrics"), metricCharts(metrics)),
    h("section", { class: "panel" }, h("h2", null, "Hyperparameters"), json(run.hyperparameters)));
}

export async function handoversPage(ctx, state = "HANDOVER_PENDING") {
  const head = pageHead("Handovers", "Requests to transfer a project's research artifacts to you.");
  const bar = tabs([["HANDOVER_PENDING", "Pending"], ["HANDOVER_APPROVED", "Approved"], ["HANDOVER_REJECTED", "Rejected"], ["HANDOVER_CANCELLED", "Cancelled"]],
    state, (s) => handoversPage(ctx, s));
  clear(ctx.view, head, bar, loading());
  const [list, projects] = await Promise.all([api(`/mlflow/handovers?status=${state}`), api("/mlflow/professor/projects")]);
  const names = new Map(projects.map((p) => [p.project_id, p.name]));
  clear(ctx.view, head, bar, list.length ? list.map((x) => handoverCard(ctx, x, names, state))
    : empty(state === "HANDOVER_PENDING" ? "Nothing to review" : "None here"));
}

function handoverCard(ctx, x, names, state) {
  const total = x.artifact_inventory.reduce((sum, a) => sum + (Number(a.size_bytes) || 0), 0);
  const name = names.get(x.project_id) || x.project_id;
  const decide = async (action) => {
    const approve = action === "approve";
    const ok = await confirm(approve ? "Take ownership of this project?" : "Reject this handover?",
      h("p", null, approve ? `Every artifact is verified first. If all pass, ${name} becomes yours.` : "The project stays with its owner."),
      approve ? "Approve handover" : "Reject", approve ? "primary" : "danger");
    if (!ok) return;
    try {
      const done = await api(`/mlflow/handovers/${x.handover_id}/${action}`, { method: "POST" });
      toast(done.failed_artifacts.length ? `Not approved: ${done.failed_artifacts.length} artifacts failed verification.` : `Handover ${action === "approve" ? "approved" : "rejected"}.`,
        done.failed_artifacts.length ? "bad" : "good");
    } catch (e) { toast(e.message, "bad"); }
    handoversPage(ctx, state);
  };
  return h("article", { class: "card" },
    h("div", { class: "card-head" }, status(x.status), h("strong", null, name), h("span", { class: "muted" }, when(x.created_at))),
    h("p", null, `${x.artifact_inventory.length} artifacts, ${bytes(total)}.`),
    x.failed_artifacts.length ? h("p", { class: "notice" }, `${x.failed_artifacts.length} artifacts failed verification.`) : null,
    x.status === "HANDOVER_PENDING" ? h("div", { class: "actions end" },
      h("button", { type: "button", class: "ghost", onclick: () => decide("reject") }, "Reject…"),
      h("button", { type: "button", class: "primary", onclick: () => decide("approve") }, "Approve…")) : null);
}
