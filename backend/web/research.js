// The researcher dashboard for Students and Researchers: their own jobs (FR-GPU-154, 157),
// sponsorship quota (FR-GPU-178), projects, versions, runs, rebuilds and IP handover (FR-ML-008).
import { api, h, clear, when, empty, loading, pageHead, toast, confirm } from "/app/steward/lib.js";
import {
  ACTIVE_JOB, artifactText, bytes, field, formStatus, json, kv, metricCharts, select, stamp, status, table, words,
} from "./ui.js";

const jobRow = (j) => [
  h("a", { href: `#/jobs/${j.id}` }, `Job ${j.id}`), status(j.state), j.queue_reason ? words(j.queue_reason) : "–", String(j.gpus), j.walltime,
  j.node_name ? `${j.node_name} (${j.unit_indexes.join(", ")})` : "–", when(j.submitted_at),
];
const JOB_HEADERS = ["Job", "State", "Waiting for", "GPUs", "Walltime", "Node (units)", "Submitted"];

export async function dashboard(ctx) {
  const head = pageHead("Dashboard", "Your GPU jobs and projects at a glance.");
  clear(ctx.view, head, loading());
  const [access, jobs, projects, quota, proposals] = await Promise.all([
    api("/access-requests/me"), api("/jobs?limit=20"), api("/mlflow/projects"),
    api("/jobs/quota").catch((e) => { if (e.status === 404) return null; throw e; }),
    ctx.steward ? api("/steward/proposals?state=PROPOSED") : [],
  ]);
  const count = (...states) => jobs.filter((j) => states.includes(j.state)).length;
  const approved = access.some((r) => r.final_status === "APPROVED");
  clear(ctx.view, head,
    approved ? null : h("p", { class: "principle" }, access.length
      ? "Your GPU access is not active yet. " : "You need approved GPU access before you can submit jobs. ",
    h("a", { href: "#/access" }, access.length ? "See your request" : "Request access")),
    h("div", { class: "stats" },
      stat("Running", count("RUNNING"), "#/jobs"), stat("Waiting", count("IN_PROCESS", "QUEUED", "SCHEDULED"), "#/jobs"),
      stat("Projects", projects.length, "#/projects"),
      quota ? stat("GPU-hours left", quota.remaining_gpu_hours, "#/") : null,
      ctx.steward ? stat("Fixes to decide", proposals.length, "#/proposals", proposals.length ? "warn" : null) : null),
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", null, "Recent jobs"), jobs.length ? h("a", { href: "#/jobs" }, "All jobs") : null),
      jobs.length ? table(JOB_HEADERS, jobs.slice(0, 6).map(jobRow)) : empty("No jobs yet", "Queue a run from one of your projects.", h("a", { class: "button ghost", href: "#/projects" }, "Go to projects"))),
    quota ? quotaPanel(quota) : null);
}

function stat(label, value, href, tone) {
  return h("a", { class: `stat ${tone || ""}`, href }, h("span", { class: "stat-value" }, String(value)), h("span", { class: "stat-label" }, label));
}

function quotaPanel(q) {
  return h("section", { class: "card" }, h("h2", null, "Course GPU-hours"),
    kv([["Cap", String(q.cap_gpu_hours)], ["Consumed", String(q.consumed_gpu_hours)], ["Committed by unfinished jobs", String(q.committed_gpu_hours)], ["Remaining", String(q.remaining_gpu_hours)]]),
    q.members.length ? table(["Member", "GPU-hours used"], q.members.map((m) => [m.user_id, String(m.consumed_gpu_hours)]), { compact: true }) : null);
}

// ── jobs ────────────────────────────────────────────────────────────────

export async function jobsPage(ctx) {
  const head = pageHead("Jobs", "Every GPU job you submitted, newest first.");
  clear(ctx.view, head, loading());
  const jobs = await api("/jobs");
  clear(ctx.view, head, jobs.length ? table(JOB_HEADERS, jobs.map(jobRow))
    : empty("No jobs yet", "Queue a run from one of your projects.", h("a", { class: "button ghost", href: "#/projects" }, "Go to projects")));
}

export async function jobDetail(ctx, id) {
  const back = h("a", { href: "#/jobs", class: "back" }, "← Jobs");
  clear(ctx.view, back, loading());
  const j = await api(`/jobs/${id}`);
  const refresh = () => jobDetail(ctx, id);
  const cancel = async () => {
    const ok = await confirm(`Cancel job ${id}?`, h("p", null, j.state === "RUNNING"
      ? "The program is stopped. The job becomes cancelled once its node confirms." : "It leaves the queue and uses no GPUs."), "Cancel job", "danger");
    if (!ok) return;
    try { toast((await api(`/jobs/${id}/cancel`, { method: "POST" })).message); } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  clear(ctx.view, back,
    pageHead(`Job ${id}`, j.queue_reason ? `Waiting: ${words(j.queue_reason)}` : null, status(j.state),
      h("button", { type: "button", class: "ghost", onclick: refresh }, "Refresh"),
      ACTIVE_JOB.includes(j.state) ? h("button", { type: "button", class: "danger", onclick: cancel }, "Cancel job") : null),
    j.planned_stop_at ? h("p", { class: "notice" }, `A maintenance window stops this job at ${stamp(j.planned_stop_at)}${j.planned_stop_reason ? `: ${j.planned_stop_reason}` : ""}.`) : null,
    j.stop_reason ? h("p", { class: "notice" }, `Stopping: ${j.stop_reason}`) : null,
    h("section", { class: "card" }, kv([
      ["GPUs", String(j.gpus)], ["Walltime", j.walltime], ["Submitted", stamp(j.submitted_at)],
      j.node_name ? ["Placed on", `${j.node_name}, units ${j.unit_indexes.join(", ")}`] : null,
      j.p0_free_at ? ["Own-lab units free by", stamp(j.p0_free_at)] : null,
      j.started_at ? ["Started", stamp(j.started_at)] : null,
      j.walltime_ends_at ? ["Walltime ends", stamp(j.walltime_ends_at)] : null,
      j.ended_at ? ["Ended", `${stamp(j.ended_at)} (${j.end_state})`] : null,
      j.exit_code !== null ? ["Exit code", String(j.exit_code)] : null,
      j.cancel_reason ? ["Cancel reason", j.cancel_reason] : null,
      j.dispatch_attempts ? ["Dispatch attempts", String(j.dispatch_attempts)] : null,
    ])),
    j.state === "QUEUED" ? editForm(ctx, j) : null);
}

function editForm(ctx, j) {
  const note = formStatus();
  return h("section", { class: "card" }, h("h2", null, "Change this job"),
    h("p", { class: "muted small" }, "Growing the request moves it to the back of the queue."),
    h("form", {
      class: "grid", onsubmit: async (event) => {
        event.preventDefault();
        const f = event.target.elements;
        try {
          await api(`/jobs/${j.id}`, { method: "PATCH", body: { gpus: Number(f.gpus.value), walltime: f.walltime.value } });
          toast("Job updated.", "good");
          jobDetail(ctx, j.id);
        } catch (e) { note.textContent = e.message; }
      },
    }, field("GPUs", h("input", { name: "gpus", type: "number", min: "1", value: String(j.gpus), required: true })),
    field("Walltime (HH:MM)", h("input", { name: "walltime", value: j.walltime, pattern: "\\d{1,3}:\\d{2}", required: true })),
    h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Save"), note)));
}

// ── projects ────────────────────────────────────────────────────────────

export async function projectsPage(ctx) {
  const head = pageHead("Projects", "A project holds your code versions and every run of them.");
  clear(ctx.view, head, loading());
  const list = await api("/mlflow/projects");
  const note = formStatus();
  const form = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      note.textContent = "Creating…";
      try {
        const body = { name: f.name.value };
        if (f.description.value.trim()) body.description = f.description.value.trim();
        const project = await api("/mlflow/projects", { method: "POST", body });
        ctx.go(`#/projects/${project.project_id}`);
      } catch (e) { note.textContent = e.message; }
    },
  }, field("Name", h("input", { name: "name", maxlength: "120", required: true })),
  field("Description (optional)", h("input", { name: "description", maxlength: "4000" })),
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Create project"), note));
  clear(ctx.view, head, list.length
    ? table(["Project", "Description", "Created"], list.map((p) => [h("a", { href: `#/projects/${p.project_id}` }, p.name), p.description || "–", when(p.created_at)]))
    : empty("No projects yet", "Create one here, or with the prayog CLI."),
  h("details", { class: "panel collapsible", open: !list.length }, h("summary", null, "Create a project"), form));
}

const runHref = (pid, r) => `#/projects/${pid}/runs/${encodeURIComponent(r.version)}/${r.run_number}`;

export async function projectDetail(ctx, pid) {
  const back = h("a", { href: "#/projects", class: "back" }, "← Projects");
  clear(ctx.view, back, loading());
  const [project, versions, runs, handovers, professors] = await Promise.all([
    api(`/mlflow/projects/${pid}/status`), api(`/mlflow/projects/${pid}/versions`), api(`/mlflow/projects/${pid}/runs`),
    api("/mlflow/handovers"), api("/professors"),
  ]);
  const refresh = () => projectDetail(ctx, pid);
  const checkout = async (version) => {
    try { await api(`/mlflow/projects/${pid}/current`, { method: "PUT", body: { version } }); toast(`Version ${version} is now current.`, "good"); } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  const clearData = async () => {
    const ok = await confirm("Delete staged data?", h("p", null, `This removes the temporary dataset copies for this project (${bytes(project.staged_bytes)}). Files a running job needs are kept. Rebuilds need the data uploaded again.`), "Delete", "danger");
    if (!ok) return;
    try {
      const done = await api(`/mlflow/projects/${pid}/staged-data`, { method: "DELETE" });
      toast(`Deleted ${done.deleted_files} files, freed ${bytes(done.freed_bytes)}.`, "good");
    } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  const mine = handovers.filter((x) => x.project_id === pid);
  clear(ctx.view, back,
    pageHead(project.name, `${project.versions} versions · ${project.runs} runs${project.current_version ? ` · version ${project.current_version} is current` : ""}`),
    h("section", { class: "panel" }, h("h2", null, "Runs"),
      runs.length ? table(["Run", "Status", "GPUs", "Walltime", "Rebuild of", "Created"], runs.map((r) => [
        h("a", { href: runHref(pid, r) }, r.label), status(r.status), String(r.gpus), r.walltime, r.rebuild_of || "–", when(r.created_at),
      ])) : empty("No runs yet", versions.length ? "Queue the first one below." : "Upload a version first.")),
    versions.length ? runForm(ctx, pid, versions) : null,
    h("section", { class: "panel" }, h("h2", null, "Versions"),
      versions.length ? table(["Version", "Files", "Runs", "Hash", "Created", ""], versions.map((v) => [
        h("span", null, v.version, v.is_current ? " " : null, v.is_current ? status("CURRENT") : null),
        String(v.file_count), String(v.run_count), h("code", { title: v.manifest_hash }, v.manifest_hash.slice(0, 12) + "…"), when(v.created_at),
        v.is_current ? "" : h("button", { type: "button", class: "ghost small", onclick: () => checkout(v.version) }, "Make current"),
      ])) : empty("No versions yet", `Versions are uploaded from your project folder: prayog project link ${project.name} <folder>`)),
    project.staged_files ? h("section", { class: "card" }, h("h2", null, "Staged data"),
      h("p", null, `${project.staged_files} files, ${bytes(project.staged_bytes)}. These temporary copies are for your runs to read.`),
      h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: clearData }, "Delete staged data"))) : null,
    handoverPanel(ctx, pid, mine, professors));
}

function runForm(ctx, pid, versions) {
  const note = formStatus();
  const current = versions.find((v) => v.is_current);
  const form = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      let hyperparameters;
      try {
        hyperparameters = f.hp.value.trim() ? JSON.parse(f.hp.value) : {};
      } catch (_) { note.textContent = "Hyperparameters must be valid JSON."; return; }
      note.textContent = "Queuing…";
      try {
        const run = await api(`/mlflow/projects/${pid}/runs`, {
          method: "POST",
          body: {
            version: f.version.value, gpus: Number(f.gpus.value), walltime: f.walltime.value, hyperparameters,
            entrypoint: f.entrypoint.value.trim() || null, client_request_id: crypto.randomUUID(),
          },
        });
        toast(`${run.label} queued as job ${run.job_id}.`, "good");
        ctx.go(runHref(pid, run));
      } catch (e) { note.textContent = e.message; }
    },
  },
  field("Version", select("version", versions.map((v) => [v.version, `Version ${v.version}`]), current?.version)),
  field("GPUs", h("input", { name: "gpus", type: "number", min: "1", value: "1", required: true })),
  field("Walltime (HH:MM)", h("input", { name: "walltime", value: "01:00", pattern: "\\d{1,3}:\\d{2}", required: true })),
  field("Entry point", h("input", { name: "entrypoint", placeholder: "main.py" })),
  h("label", { class: "field wide-field" }, h("span", null, "Hyperparameters (JSON, optional)"),
    h("textarea", { name: "hp", rows: "2", spellcheck: "false", placeholder: "{\"lr\": 0.01}" })),
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Queue run"), note),
  h("p", { class: "muted small wide-field" }, "Runs with a dataset are submitted from the CLI, which fingerprints and uploads the files."));
  return h("details", { class: "panel collapsible" }, h("summary", null, "Queue a run"), form);
}

function handoverPanel(ctx, pid, mine, professors) {
  const note = formStatus();
  const pending = mine.find((x) => x.status === "HANDOVER_PENDING");
  const cancel = async () => {
    try { await api(`/mlflow/handovers/${pending.handover_id}/cancel`, { method: "POST" }); toast("Handover cancelled."); } catch (e) { toast(e.message, "bad"); }
    projectDetail(ctx, pid);
  };
  const push = async (event) => {
    event.preventDefault();
    const professor = event.target.elements.professor;
    const name = professor.options[professor.selectedIndex].text;
    const ok = await confirm("Hand this project over?", h("p", null, `${name} is asked to take ownership of this project's research artifacts. Nothing moves until they approve.`), "Send request");
    if (!ok) return;
    try {
      await api(`/mlflow/projects/${pid}/handover`, { method: "POST", body: { professor_id: Number(professor.value) } });
      toast("Handover requested.", "good");
      projectDetail(ctx, pid);
    } catch (e) { note.textContent = e.message; }
  };
  return h("details", { class: "panel collapsible", open: Boolean(pending) }, h("summary", null, "IP handover"),
    mine.length ? table(["Requested", "Status"], mine.map((x) => [when(x.created_at), status(x.status)]), { compact: true }) : null,
    pending ? h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: cancel }, "Cancel the request"))
      : h("form", { class: "grid", onsubmit: push },
        field("Hand over to your supervising Professor", select("professor", professors.map((p) => [String(p.id), `${p.name} (${p.department})`]))),
        h("div", { class: "form-foot" }, h("button", { type: "submit", class: "ghost" }, "Request handover…"), note)));
}

// ── one run ─────────────────────────────────────────────────────────────

export async function gpuRunDetail(ctx, pid, version, number) {
  const v = decodeURIComponent(version);
  const back = h("a", { href: `#/projects/${pid}`, class: "back" }, "← Project");
  clear(ctx.view, back, loading());
  const run = await api(`/mlflow/projects/${pid}/runs/${encodeURIComponent(v)}/${number}`);
  const metrics = await api(`/mlflow/runs/${run.run_id}/metrics`);
  const refresh = () => gpuRunDetail(ctx, pid, version, number);
  const logBox = h("div");
  const showLog = async () => {
    clear(logBox, loading());
    try { clear(logBox, h("pre", { class: "block log" }, await artifactText(`/mlflow/artifacts/${run.logs_artifact_id}/content`))); } catch (e) { clear(logBox, h("p", { class: "notice" }, e.message)); }
  };
  clear(ctx.view, back,
    pageHead(run.label, run.rebuild_of ? `A rebuild of ${run.rebuild_of}` : null, status(run.status),
      h("a", { class: "button ghost", href: `#/jobs/${run.job_id}` }, `Job ${run.job_id}`),
      h("button", { type: "button", class: "ghost", onclick: refresh }, "Refresh")),
    run.error_summary ? h("p", { class: "notice" }, run.error_summary) : null,
    run.status === "FAILED" && ctx.steward ? h("p", { class: "principle" }, "The Steward diagnoses failed runs in the background. ", h("a", { href: "#/diagnoses" }, "See your diagnoses")) : null,
    h("section", { class: "card" }, kv([
      ["GPUs", String(run.gpus)], ["Walltime", run.walltime], ["Queued", stamp(run.created_at)],
      run.queue_reason ? ["Waiting for", words(run.queue_reason)] : null,
      run.started_at ? ["Started", stamp(run.started_at)] : null, run.finished_at ? ["Finished", stamp(run.finished_at)] : null,
      run.snapshot_id ? ["Snapshot", h("code", null, run.snapshot_id)] : null,
    ])),
    h("section", { class: "panel" }, h("h2", null, "Metrics"), metricCharts(metrics)),
    h("section", { class: "panel" }, h("h2", null, "Hyperparameters"), json(run.hyperparameters)),
    run.dataset.length ? h("section", { class: "panel" }, h("h2", null, "Dataset"),
      table(["File", "Size", "BLAKE3", "Copy held"], run.dataset.map((d) => [d.path, bytes(d.size_bytes), h("code", { title: d.blake3 }, d.blake3.slice(0, 12) + "…"), d.staged ? "Yes" : "No"]), { compact: true })) : null,
    run.checkpoints.length ? h("section", { class: "panel" }, h("h2", null, "Checkpoints"),
      table(["Step", "Size", ""], run.checkpoints.map((c) => [String(c.step), bytes(c.size_bytes), c.is_best ? status("BEST") : ""]), { compact: true })) : null,
    run.logs_artifact_id ? h("section", { class: "panel" }, h("h2", null, "Log"), h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: showLog }, "Show log")), logBox) : null,
    run.rebuild_of ? await comparison(pid, v, number) : null,
    run.status === "SUCCEEDED" && run.snapshot_id ? rebuildForm(ctx, pid, run) : null);
}

function rebuildForm(ctx, pid, run) {
  const note = formStatus();
  return h("details", { class: "panel collapsible" }, h("summary", null, "Rebuild this run"),
    h("p", { class: "muted small" }, "Runs the same version, hyperparameters and data again, as an ordinary job with no priority advantage."),
    h("form", {
      class: "grid", onsubmit: async (event) => {
        event.preventDefault();
        const f = event.target.elements;
        note.textContent = "Queuing…";
        try {
          const rebuilt = await api(`/mlflow/projects/${pid}/runs`, {
            method: "POST",
            body: {
              rebuild_of: `${run.version}/${run.run_number}`, gpus: Number(f.gpus.value), walltime: f.walltime.value,
              client_request_id: crypto.randomUUID(),
              dataset: run.dataset.map((d) => ({ path: d.path, size_bytes: d.size_bytes, blake3: d.blake3 })),
            },
          });
          toast(`${rebuilt.label} queued as job ${rebuilt.job_id}.`, "good");
          ctx.go(runHref(pid, rebuilt));
        } catch (e) { note.textContent = e.message; }
      },
    }, field("GPUs", h("input", { name: "gpus", type: "number", min: "1", value: String(run.gpus), required: true })),
    field("Walltime (HH:MM)", h("input", { name: "walltime", value: run.walltime, pattern: "\\d{1,3}:\\d{2}", required: true })),
    h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Queue rebuild"), note)));
}

async function comparison(pid, version, number) {
  const report = await api(`/mlflow/projects/${pid}/runs/${encodeURIComponent(version)}/${number}/compare`).catch(() => null);
  if (!report) return null;
  return h("section", { class: "panel" }, h("h2", null, `Compared with ${report.original}`),
    h("p", null, status(report.reproduced ? "REPRODUCED" : "NOT_REPRODUCED"), " ",
      report.weights.identical ? "The weights are identical, byte for byte." : "The saved weights differ or are missing."),
    report.metrics.length ? h("p", { class: "muted" }, `${report.metrics.filter((m) => m.within_tolerance).length} of ${report.metrics.length} metric values match.`) : null,
    report.metrics.length ? h("details", { class: "collapsible" }, h("summary", null, "Every metric value"),
      table(["Metric", "Step", "Original", "Rebuilt", "Difference", "Matches"], report.metrics.map((m) => [
        m.name, String(m.step), String(m.original), m.rebuilt === null ? "Not logged" : String(m.rebuilt),
        m.difference === null ? "–" : String(m.difference), m.within_tolerance ? "Yes" : "No",
      ]), { compact: true })) : null);
}
