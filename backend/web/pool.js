// The admin dashboard. The pool picture is read-only (FR-GPU-156); every operation lives on
// the node and maintenance pages (FR-GPU-175). Metadata only: no project content anywhere here.
import { api, h, clear, confirm, when, empty, loading, pageHead, toast } from "/app/steward/lib.js";
import { askReason, bytes, field, formStatus, kv, localIn, select, stamp, status, table, toUtc, withCollisionCheck, words } from "./ui.js";

const UNIT_LABEL = { FREE: "Free", ALLOCATED: "In use", MAINTENANCE: "Maintenance", RETIRED: "Retired" };

function unitLegend() {
  return h("p", { class: "unit-legend" }, Object.entries(UNIT_LABEL).map(([state, label]) =>
    h("span", null, h("span", { class: `unit ${state.toLowerCase()}`, "aria-hidden": "true" }), label)));
}

function unitGrid(node) {
  return h("div", { class: "units" }, node.units.map((u) => h("span", {
    class: `unit ${u.state.toLowerCase()}`, role: "img",
    "aria-label": `GPU ${u.index}: ${UNIT_LABEL[u.state] || u.state}`,
    title: `GPU ${u.index}: ${UNIT_LABEL[u.state] || u.state}${u.job_id ? `, job ${u.job_id}` : ""}${u.maintenance_until ? `, until ${stamp(u.maintenance_until)}` : ""}`,
  })));
}

export async function poolPage(ctx) {
  const head = pageHead("Pool", "The operational picture, read-only. Actions are on Nodes and Maintenance.",
    h("button", { type: "button", class: "ghost", onclick: () => poolPage(ctx) }, "Refresh"));
  clear(ctx.view, head, loading());
  const p = await api("/admin/pool?audit_limit=30");
  const u = p.utilisation;
  const warnings = p.hardware_warnings ? p.hardware_warnings.OPEN || 0 : null;
  clear(ctx.view, head,
    h("div", { class: "stats" },
      tile(`${u.allocated_units} / ${u.usable_units}`, "GPUs in use"),
      tile(`${Math.round(u.allocated_fraction * 100)}%`, "Allocated"),
      tile(String(u.gpu_hours_last_24h), "GPU-hours, last 24 h"),
      tile(String(p.queue.by_state.QUEUED || 0), "Jobs queued"),
      tile(String(p.alerts.length), "Open alerts", p.alerts.length ? "bad" : null),
      warnings === null ? null : tile(String(warnings), "Hardware warnings", warnings ? "bad" : null)),
    h("section", { class: "panel" }, h("h2", null, "Nodes"), unitLegend(),
      p.nodes.length ? h("div", { class: "cards" }, p.nodes.map((n) => h("article", { class: "card" },
        h("div", { class: "card-head" }, h("strong", null, n.name), status(n.state), h("span", { class: "muted" }, n.last_heartbeat_at ? when(n.last_heartbeat_at) : "No heartbeat")),
        unitGrid(n),
        h("p", { class: "muted small" }, `Lab ${n.owner_lab_id} · ${n.donation_mode}${n.teaching_pool ? " · teaching pool" : ""}${n.open_alerts ? ` · ${n.open_alerts} alerts` : ""}`))))
        : empty("No nodes registered")),
    h("section", { class: "panel" }, h("h2", null, "Queue"),
      Object.keys(p.queue.by_state).length ? kv(Object.entries(p.queue.by_state).map(([k, v]) => [words(k), String(v)])) : h("p", { class: "muted" }, "Empty."),
      p.queue.by_lab.length ? table(["Lab", "Jobs queued", "GPUs requested"], p.queue.by_lab.map((l) => [String(l.lab_id), String(l.queued_jobs), String(l.queued_gpus)]), { compact: true }) : null),
    p.alerts.length ? h("section", { class: "panel" }, h("h2", null, "Open alerts"),
      table(["Node", "Kind", "Detail", "Raised"], p.alerts.map((a) => [String(a.node_id), words(a.kind), a.detail, when(a.raised_at)]), { compact: true })) : null,
    h("section", { class: "panel" }, h("h2", null, "Audit log"),
      p.audit.length ? table(["When", "Actor", "Operation", "Resources", "Outcome", "Reason"], p.audit.map((a) => [
        when(a.at), a.actor, a.operation, a.resource_ids.join(", ") || "–", a.outcome, a.reason || "–",
      ]), { compact: true }) : empty("No audit entries")));
}

function tile(value, label, tone) {
  return h("div", { class: `stat ${tone || ""}` }, h("span", { class: "stat-value" }, value), h("span", { class: "stat-label" }, label));
}

// ── nodes ───────────────────────────────────────────────────────────────

export async function nodesPage(ctx) {
  const head = pageHead("Nodes", "Register nodes, change their state, and clear alerts.");
  clear(ctx.view, head, loading());
  const [nodes, alerts] = await Promise.all([api("/admin/nodes"), api("/admin/alerts")]);
  clear(ctx.view, head,
    alerts.length ? h("section", { class: "panel" }, h("h2", null, "Open alerts"), alerts.map((a) => h("article", { class: "card" },
      h("div", { class: "card-head" }, h("strong", null, words(a.kind)), h("span", null, `Node ${a.node_id}`), h("span", { class: "muted" }, when(a.raised_at))),
      h("p", null, a.detail),
      h("div", { class: "actions end" }, h("button", {
        type: "button", class: "ghost", onclick: async () => {
          const reason = await askReason("Acknowledge this alert?", a.detail, "Acknowledge", "primary");
          if (!reason) return;
          try { await api(`/admin/alerts/${a.id}/acknowledge`, { method: "POST", body: { reason } }); toast("Alert acknowledged."); } catch (e) { toast(e.message, "bad"); }
          nodesPage(ctx);
        },
      }, "Acknowledge…"))))) : null,
    nodes.length ? table(["Node", "State", "Free / total", "Lab", "Sharing", "Heartbeat", "Alerts"], nodes.map((n) => [
      h("a", { href: `#/nodes/${n.id}` }, n.name), status(n.state), `${n.free_units} / ${n.unit_count}`, String(n.owner_lab_id),
      `${n.donation_mode}${n.teaching_pool ? ", teaching" : ""}`, n.last_heartbeat_at ? when(n.last_heartbeat_at) : "Never", String(n.open_alerts),
    ])) : empty("No nodes registered"),
    registerForm(ctx), cancelJobForm(ctx));
}

function registerForm(ctx) {
  const note = formStatus();
  const out = h("div");
  const form = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      note.textContent = "Registering…";
      try {
        const reg = await api("/admin/nodes", {
          method: "POST",
          body: {
            name: f.name.value, owner_lab_id: Number(f.lab.value), donation_mode: f.mode.value,
            teaching_pool: f.teaching.checked, unit_count: Number(f.units.value),
          },
        });
        note.textContent = "";
        form.reset();
        // Shown once: valid for 24 hours and for one enrolment.
        clear(out, h("div", { class: "notice" }, h("p", null, `${reg.node.name} registered. Give this one-time token to its agent:`),
          h("pre", { class: "block" }, reg.enrolment_token), h("p", { class: "small" }, "It is shown only once and expires in 24 hours.")));
      } catch (e) { note.textContent = e.message; }
    },
  },
  field("Name", h("input", { name: "name", maxlength: "100", required: true })),
  field("Owner lab ID", h("input", { name: "lab", type: "number", min: "1", required: true })),
  field("GPUs", h("input", { name: "units", type: "number", min: "1", max: "64", required: true })),
  field("Donation", select("mode", [["reserved", "Reserved"], ["shared", "Shared"]], "shared")),
  h("label", { class: "check" }, h("input", { name: "teaching", type: "checkbox" }), " Teaching pool (shared nodes only)"),
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Register node"), note));
  return h("details", { class: "panel collapsible" }, h("summary", null, "Register a node"), form, out);
}

function cancelJobForm(ctx) {
  const note = formStatus();
  return h("details", { class: "panel collapsible" }, h("summary", null, "Cancel any job"),
    h("form", {
      class: "grid", onsubmit: async (event) => {
        event.preventDefault();
        const f = event.target.elements;
        const reason = await askReason(`Cancel job ${f.job.value}?`, "The owner's job is stopped. The reason is audited with your name.", "Cancel job");
        if (!reason) return;
        try {
          toast((await api(`/admin/jobs/${Number(f.job.value)}/cancel`, { method: "POST", body: { reason } })).message);
          f.job.value = "";
        } catch (e) { note.textContent = e.message; }
      },
    }, field("Job ID", h("input", { name: "job", type: "number", min: "1", required: true })),
    h("div", { class: "form-foot" }, h("button", { type: "submit", class: "danger" }, "Cancel job…"), note)));
}

export async function nodeDetail(ctx, id) {
  const back = h("a", { href: "#/nodes", class: "back" }, "← Nodes");
  clear(ctx.view, back, loading());
  const [nodes, pool] = await Promise.all([api("/admin/nodes"), api("/admin/pool?audit_limit=0")]);
  const n = nodes.find((x) => String(x.id) === String(id));
  if (!n) { clear(ctx.view, back, empty("Node not found")); return; }
  const picture = pool.nodes.find((x) => x.id === n.id);
  const refresh = () => nodeDetail(ctx, id);
  const run = (path, body, done) => async () => {
    try { await api(`/admin/nodes/${n.id}/${path}`, { method: "POST", body }); toast(done, "good"); } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  const withReason = (path, title, text, label, extra = {}) => async () => {
    const reason = await askReason(title, text, label);
    if (reason) await run(path, { reason, ...extra }, `${n.name}: done.`)();
  };
  const action = (label, onclick, tone = "ghost") => h("button", { type: "button", class: tone, onclick }, label);
  const actions = [];
  if (n.state === "AVAILABLE" || n.state === "DOWN") {
    actions.push(action("Drain (soft)…", withReason("drain", `Drain ${n.name}?`, "No new work starts here; running jobs finish.", "Drain", { mode: "soft" })));
    actions.push(action("Drain (hard)…", withReason("drain", `Hard-drain ${n.name}?`, "Running jobs are stopped after the grace period.", "Drain", { mode: "hard" }), "danger"));
  }
  if (n.state === "DRAINING" || n.state === "DOWN") actions.push(action("Return to service", run("return", undefined, "Returned; it becomes available on its next healthy heartbeat.")));
  if (n.state === "DRAINING") actions.push(action("Escalate to decommission…", withReason("escalate", `Decommission ${n.name}?`, "This drain becomes a permanent retirement.", "Escalate"), "danger"));
  if (n.state === "DECOMMISSIONING") actions.push(action("Abort decommission…", withReason("abort-decommission", "Abort the decommission?", "The node goes back to draining.", "Abort")));
  if (n.state === "AVAILABLE") actions.push(action("Decommission…", withReason("decommission", `Retire ${n.name} permanently?`, "Running jobs finish first. Its records are kept.", "Decommission", { mode: "soft" }), "danger"));

  const note = formStatus();
  const sharing = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      const reason = await askReason(`Change sharing on ${n.name}?`, "Running jobs are not stopped. The next scheduling pass uses the new settings.", "Save", "primary");
      if (!reason) return;
      try {
        await api(`/admin/nodes/${n.id}/sharing`, { method: "PATCH", body: { reason, donation_mode: f.mode.value, teaching_pool: f.teaching.checked } });
        toast("Sharing updated.", "good");
        refresh();
      } catch (e) { note.textContent = e.message; }
    },
  }, field("Donation", select("mode", [["reserved", "Reserved"], ["shared", "Shared"]], n.donation_mode)),
  h("label", { class: "check" }, h("input", { name: "teaching", type: "checkbox", checked: n.teaching_pool }), " Teaching pool"),
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "ghost" }, "Save…"), note));

  clear(ctx.view, back,
    pageHead(n.name, n.state_reason, status(n.state)),
    h("section", { class: "card" }, h("h2", null, "GPUs"), picture ? [unitGrid(picture), unitLegend()] : null,
      kv([["Free", `${n.free_units} of ${n.unit_count}`], ["Owner lab", String(n.owner_lab_id)], ["Drain mode", n.drain_mode || "–"],
        ["Heartbeat", n.last_heartbeat_at ? stamp(n.last_heartbeat_at) : "Never"],
        n.return_requested ? ["Return", "Requested"] : null])),
    n.reported_unit_count !== null && n.reported_unit_count !== n.unit_count ? h("section", { class: "card" }, h("h2", null, "Unit count changed"),
      h("p", null, `The node reports ${n.reported_unit_count} GPUs; ${n.unit_count} were declared.`),
      h("div", { class: "actions" }, action(`Confirm ${n.reported_unit_count} GPUs`, run("unit-count", { unit_count: n.reported_unit_count }, "Unit count confirmed."), "primary"))) : null,
    actions.length ? h("section", { class: "card" }, h("h2", null, "Operations"), h("div", { class: "actions" }, actions)) : null,
    h("section", { class: "card" }, h("h2", null, "Sharing"), sharing));
}

// ── maintenance ─────────────────────────────────────────────────────────

export async function maintenancePage(ctx) {
  const head = pageHead("Maintenance", "Book GPUs out of scheduling for a window. Windows that would stop running jobs need confirmation.");
  clear(ctx.view, head, loading());
  const [bookings, pool] = await Promise.all([api("/admin/maintenance"), api("/admin/pool?audit_limit=0")]);
  const names = new Map(pool.nodes.flatMap((n) => n.units.map((u) => [u.id, `${n.name}/gpu${u.index}`])));
  const cancel = (b) => async () => {
    const ok = await confirm("Cancel this window?", h("p", null, "The GPUs become eligible for scheduling again."), "Cancel window");
    if (!ok) return;
    try { await api(`/admin/maintenance/${b.booking_id}`, { method: "DELETE" }); toast("Window cancelled."); } catch (e) { toast(e.message, "bad"); }
    maintenancePage(ctx);
  };
  clear(ctx.view, head,
    bookings.length ? table(["GPUs", "From", "Until", "Reason", ""], bookings.map((b) => [
      b.unit_ids.map((id) => names.get(id) || id).join(", "), stamp(b.starts_at), stamp(b.ends_at), b.reason,
      h("button", { type: "button", class: "ghost small", onclick: cancel(b) }, "Cancel…"),
    ])) : empty("No maintenance booked"),
    bookForm(ctx, pool));
}

function bookForm(ctx, pool) {
  const note = formStatus();
  const preview = h("div");
  const form = h("form", { class: "stack" },
    h("fieldset", { class: "unit-picker" }, h("legend", null, "GPUs"), pool.nodes.map((n) => h("div", { class: "checks" },
      h("strong", null, n.name), n.units.map((u) => h("label", { class: "check" }, h("input", { type: "checkbox", name: "unit", value: String(u.id) }), ` gpu${u.index}`))))),
    h("div", { class: "grid" },
      field("Starts", h("input", { name: "start", type: "datetime-local", value: localIn(3600000), required: true })),
      field("Ends", h("input", { name: "end", type: "datetime-local", value: localIn(7200000), required: true })),
      field("Reason", h("input", { name: "reason", maxlength: "255", required: true }))),
    h("div", { class: "form-foot" }, h("button", { type: "button", class: "ghost", onclick: showPreview }, "Preview"),
      h("button", { type: "submit", class: "primary" }, "Book window"), note),
    preview);
  const chosen = () => [...form.querySelectorAll("input[name=unit]:checked")].map((i) => Number(i.value));

  async function showPreview() {
    const unit_ids = chosen();
    if (!unit_ids.length) { note.textContent = "Choose at least one GPU."; return; }
    note.textContent = "";
    try {
      const p = await api("/admin/maintenance/preview", { method: "POST", body: { unit_ids, starts_at: toUtc(form.elements.start.value) } });
      clear(preview, h("div", { class: "notice" }, h("p", null, `These GPUs are guaranteed clear by ${stamp(p.clear_by)}.`),
        p.collisions.length ? h("ul", null, p.collisions.map((c) => h("li", null, `Job ${c.job_id} (${c.owner}) would be stopped: ${c.gpu_hours_at_risk} GPU-hours at risk.`)))
          : h("p", null, "This window would stop no running job.")));
    } catch (e) { note.textContent = e.message; }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = form.elements;
    const unit_ids = chosen();
    if (!unit_ids.length) { note.textContent = "Choose at least one GPU."; return; }
    const body = { unit_ids, starts_at: toUtc(f.start.value), ends_at: toUtc(f.end.value), reason: f.reason.value };
    note.textContent = "Booking…";
    try {
      const done = await withCollisionCheck((confirmed) => api("/admin/maintenance", { method: "POST", body: { ...body, confirm: confirmed } }));
      if (!done) { note.textContent = ""; return; }
      toast("Maintenance booked.", "good");
      maintenancePage(ctx);
    } catch (e) { note.textContent = e.message; }
  });
  return h("details", { class: "panel collapsible" }, h("summary", null, "Book a window"), form);
}

// ── ledger overview ─────────────────────────────────────────────────────

export async function ledgerPage(ctx) {
  const head = pageHead("Ledger overview", "Aggregate counts only. No project content, by design.");
  clear(ctx.view, head, loading());
  const { availability: a, volume: v } = await api("/admin/mlflow/overview");
  const breakdown = (title, map) => (Object.keys(map).length ? h("section", { class: "card" }, h("h2", null, title), kv(Object.entries(map).map(([k, n]) => [words(k), String(n)]))) : null);
  clear(ctx.view, head,
    h("div", { class: "stats" }, tile(String(v.projects), "Projects"), tile(String(v.runs), "Runs"), tile(String(v.snapshots), "Snapshots"),
      tile(String(v.artifacts), "Artifacts"), tile(bytes(v.logical_artifact_bytes), "Artifact bytes")),
    h("section", { class: "card" }, h("h2", null, "Availability"), kv([["Database", a.database], ["Artifact storage", `${a.artifact_storage} (${a.artifact_storage_backend})`]])),
    breakdown("Runs by status", v.runs_by_status), breakdown("Rebuilds by state", v.rebuilds_by_state), breakdown("Outbox events", v.outbox_events_by_status));
}
