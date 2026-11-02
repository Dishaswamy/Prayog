// Pieces the platform screens share, on top of the Steward UI's client and DOM helpers.
// As there, nothing assigns innerHTML: everything from the server is text.
import { api, h, badge, clear, confirm, parseTime, token } from "/app/steward/lib.js";

const TONE = {
  IN_PROCESS: "info", QUEUED: "info", SCHEDULED: "info", RUNNING: "accent", COMPLETED: "good", SUCCEEDED: "good",
  FAILED: "bad", CANCELLED: "muted", WALLTIME_EXCEEDED: "warn", REJECTED: "bad", CREATED: "muted",
  PENDING: "warn", APPROVED: "good", REVOKED: "muted", RECORDED: "good", INCOMPLETE: "warn",
  AVAILABLE: "good", PENDING_AGENT: "warn", DRAINING: "warn", DOWN: "bad", DECOMMISSIONING: "warn", DECOMMISSIONED: "muted",
  HANDOVER_PENDING: "warn", HANDOVER_APPROVED: "good", HANDOVER_REJECTED: "bad", HANDOVER_CANCELLED: "muted",
};

export function words(state) {
  const plain = String(state || "–").replace(/^HANDOVER_/, "").replace(/_/g, " ").toLowerCase();
  return plain.charAt(0).toUpperCase() + plain.slice(1);
}

export const status = (state) => badge(words(state), TONE[state] || "muted");

export const ACTIVE_JOB = ["IN_PROCESS", "QUEUED", "SCHEDULED", "RUNNING"];

export function field(label, control, help) {
  return h("label", { class: "field" }, h("span", null, label), control,
    help ? h("small", { class: "muted" }, help) : null);
}

export function select(name, options, value) {
  return h("select", { name, required: true }, options.map(([v, label]) =>
    h("option", { value: v, selected: v === value }, label)));
}

export function formStatus() {
  return h("p", { class: "status", role: "status" });
}

export function stamp(value) {
  const date = parseTime(value);
  return date ? date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
}

export function bytes(n) {
  const size = Number(n) || 0;
  if (size < 1024) return `${size} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = size / 1024, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(1)} ${units[i]}`;
}

// The value of a datetime-local input, in the viewer's zone, for the server.
export const toUtc = (local) => new Date(local).toISOString();

// A datetime-local value `ms` from now.
export function localIn(ms) {
  const at = new Date(Date.now() + ms);
  return new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function table(headers, rows, { compact = false } = {}) {
  return h("div", { class: "table-wrap" }, h("table", { class: compact ? "table compact" : "table" },
    h("thead", null, h("tr", null, headers.map((c) => h("th", null, c)))),
    h("tbody", null, rows.map((cells) => h("tr", null, cells.map((c) => h("td", null, c)))))));
}

export function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v ?? "–")]));
}

export function json(value) {
  const text = JSON.stringify(value ?? {}, null, 2);
  return text === "{}" ? h("span", { class: "muted" }, "None") : h("pre", { class: "block" }, text);
}

// Asks for the mandatory reason of an admin action. Resolves to it, or null when cancelled.
export async function askReason(title, text, label, tone = "danger") {
  const input = h("textarea", { rows: "2", maxlength: "255", required: true, "aria-label": "Reason" });
  const ok = await confirm(title, [h("p", null, text), field("Reason (recorded)", input)], label, tone);
  return ok && input.value.trim() ? input.value.trim() : null;
}

// Runs a request that the server refuses (409, with `collisions`) when it would stop running
// jobs: shows them, then resends with confirm only if the admin agrees (FR-GPU-084).
export async function withCollisionCheck(send) {
  try {
    return await send(false);
  } catch (e) {
    if (e.status !== 409 || !Array.isArray(e.body.collisions)) throw e;
    const ok = await confirm("Running jobs would be stopped", [
      h("p", null, e.message),
      h("ul", null, e.body.collisions.map((c) => h("li", null, `Job ${c.job_id} (${c.owner}): ${c.gpu_hours_at_risk} GPU-hours at risk`))),
    ], "Go ahead", "danger");
    return ok ? send(true) : null;
  }
}

// An artifact's bytes as text; artifact routes need the bearer token, so a plain link cannot do.
export async function artifactText(path) {
  const response = await fetch(path, { headers: { Authorization: "Bearer " + token.get() }, credentials: "same-origin" });
  if (!response.ok) throw new Error("The server could not return this file.");
  return response.text();
}

// ── metric charts: one small chart per metric, so each keeps its own scale ──

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}

const number = (v) => (Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01) ? v.toExponential(2) : String(+v.toFixed(4)));

function chart(name, points) {
  const W = 320, H = 132, L = 46, R = 10, T = 10, B = 22;
  const steps = points.map((p) => p.step), values = points.map((p) => p.value);
  const [x0, x1] = [Math.min(...steps), Math.max(...steps)];
  let [y0, y1] = [Math.min(...values), Math.max(...values)];
  if (y0 === y1) { y0 -= Math.abs(y0) * 0.1 || 1; y1 += Math.abs(y1) * 0.1 || 1; }
  const px = (s) => (x0 === x1 ? (L + W - R) / 2 : L + ((s - x0) / (x1 - x0)) * (W - L - R));
  const py = (v) => H - B - ((v - y0) / (y1 - y0)) * (H - T - B);

  const label = (x, y, anchor, text) => svg("text", { x, y, "text-anchor": anchor, class: "chart-tick" }, document.createTextNode(text));
  const cross = svg("line", { y1: T, y2: H - B, class: "chart-cross", visibility: "hidden" });
  const dot = svg("circle", { r: 4, class: "chart-dot" });
  const last = points[points.length - 1];
  dot.setAttribute("cx", px(last.step));
  dot.setAttribute("cy", py(last.value));

  const readout = h("span", { class: "chart-readout" }, `step ${last.step}: ${number(last.value)}`);
  const plot = svg("svg", {
    viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
    "aria-label": `${name}: ${points.length} points, from ${number(points[0].value)} to ${number(last.value)}`,
  },
  svg("line", { x1: L, x2: W - R, y1: T, y2: T, class: "chart-grid" }),
  svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, class: "chart-axis" }),
  label(L - 6, T + 4, "end", number(y1)), label(L - 6, H - B + 4, "end", number(y0)),
  label(L, H - 6, "start", `step ${x0}`), x1 !== x0 ? label(W - R, H - 6, "end", `step ${x1}`) : null,
  points.length > 1 ? svg("polyline", { class: "chart-line", points: points.map((p) => `${px(p.step)},${py(p.value)}`).join(" ") }) : null,
  cross, dot);

  plot.addEventListener("pointermove", (event) => {
    const box = plot.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * W;
    const near = points.reduce((a, b) => (Math.abs(px(b.step) - x) < Math.abs(px(a.step) - x) ? b : a));
    cross.setAttribute("x1", px(near.step)); cross.setAttribute("x2", px(near.step));
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px(near.step)); dot.setAttribute("cy", py(near.value));
    readout.textContent = `step ${near.step}: ${number(near.value)}`;
  });
  plot.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("cx", px(last.step)); dot.setAttribute("cy", py(last.value));
    readout.textContent = `step ${last.step}: ${number(last.value)}`;
  });
  return h("figure", { class: "chart-card" }, h("figcaption", null, h("strong", null, name), readout), plot);
}

export function metricCharts(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "No metrics recorded.");
  const groups = new Map();
  for (const r of rows) {
    if (!groups.has(r.name)) groups.set(r.name, []);
    groups.get(r.name).push({ step: r.step, value: r.value });
  }
  const names = [...groups.keys()].sort();
  for (const name of names) groups.get(name).sort((a, b) => a.step - b.step);
  return h("div", { class: "stack" },
    h("div", { class: "charts" }, names.map((n) => chart(n, groups.get(n)))),
    h("details", { class: "collapsible" }, h("summary", null, "Metrics as a table"),
      table(["Metric", "Step", "Value"], names.flatMap((n) => groups.get(n).map((p) => [n, String(p.step), String(p.value)])), { compact: true })));
}

export { api, clear };
