// Shared pieces of the Steward web UI: the API client, DOM building and the renderers
// that more than one view uses. Nothing here ever assigns innerHTML: model output, logs
// and record ids are always text, so none of it can inject markup or script (FR-STW-016).

// ── the API client ──────────────────────────────────────────────────────

// Prayog's /auth/login returns a bearer token. It lives only in this tab's sessionStorage.
const TOKEN_KEY = "steward_bearer";

export const token = {
  get() { try { return sessionStorage.getItem(TOKEN_KEY); } catch (_) { return null; } },
  set(value) { try { sessionStorage.setItem(TOKEN_KEY, value); } catch (_) { /* private mode */ } },
  clear() { try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* private mode */ } },
};

export class HttpError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body || {};
  }
}

// Every POST carries X-Steward-CSRF: cookie-authenticated Steward mutations need it, and
// the platform ignores it.
export async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Steward-CSRF": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const bearer = token.get();
  if (bearer) headers.Authorization = "Bearer " + bearer;
  const response = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    throw new HttpError(response.status, errorText(response.status, data), data);
  }
  return data;
}

function errorText(status, data) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0] && detail[0].msg) return detail[0].msg;  // validation
  if (status === 404) return "Not found, or not yours to see.";
  if (status >= 500) return "The server could not answer. Try again in a moment.";
  return "The request failed.";
}

// ── DOM building ────────────────────────────────────────────────────────

// h("a", {class: "x", href: "#/"}, "text", child): strings become text nodes.
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

// ── formatting ──────────────────────────────────────────────────────────

// /rebuilds sends naive UTC; /steward sends an explicit offset.
export function parseTime(value) {
  if (!value) return null;
  const text = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function ago(value) {
  const date = parseTime(value);
  if (!date) return "–";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  const span = Math.abs(seconds);
  const [n, unit] = span < 60 ? [span, "s"] : span < 3600 ? [Math.round(span / 60), "min"]
    : span < 86400 ? [Math.round(span / 3600), "h"] : [Math.round(span / 86400), "d"];
  return seconds >= 0 ? `${n} ${unit} ago` : `in ${n} ${unit}`;
}

export function when(value) {
  const date = parseTime(value);
  return h("time", { datetime: date ? date.toISOString() : null, title: date ? date.toLocaleString() : null },
    ago(value));
}

// Project ids are scoped to their owner ("u<user id>:<project_ref>"); show the ref.
export function projectName(projectId) {
  if (!projectId) return "–";
  const ref = projectId.replace(/^u\d+:/, "");
  return ref || "(no project)";
}

export function minutes(value) {
  const m = Number(value);
  if (!Number.isFinite(m)) return String(value);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim() : `${m} min`;
}

export const CLASSIFICATION = {
  oom: "Out of memory",
  missing_data: "Missing data",
  library_version: "Library version",
  code_bug: "Code bug",
  interruption: "Interruption",
  hardware: "Hardware",
  unknown: "Unclassified",
};

export const VERDICT = {
  likely_hardware: ["Likely hardware", "bad"],
  unlikely_hardware: ["Unlikely hardware", "good"],
  inconclusive: ["Inconclusive", "warn"],
};

const STATE_TONE = {
  PROPOSED: "warn", APPROVED: "good", REJECTED: "muted",
  REBUILD_INITIATED: "info", REBUILD_SUCCESS: "good", REBUILD_FAILED: "bad", REBUILD_CANCELLED: "muted",
  OPEN: "bad", RESOLVED: "good", DISMISSED: "muted",
  ok: "good", refused: "warn", malformed: "warn", unavailable: "warn",
};

const STATE_LABEL = {
  PROPOSED: "Awaiting your decision", REBUILD_INITIATED: "Running", REBUILD_SUCCESS: "Succeeded",
  REBUILD_FAILED: "Failed", REBUILD_CANCELLED: "Cancelled",
};

export function badge(text, tone = "muted") {
  return h("span", { class: `badge ${tone}` }, text);
}

export function stateBadge(state) {
  const plain = state.replace(/_/g, " ").toLowerCase();
  return badge(STATE_LABEL[state] || plain.charAt(0).toUpperCase() + plain.slice(1), STATE_TONE[state] || "muted");
}

export function classificationBadge(value) {
  return badge(CLASSIFICATION[value] || value || "Unclassified", value === "hardware" ? "bad" : "accent");
}

// ── shared renderers ────────────────────────────────────────────────────

// A cite is a record id string ("log:3:0#0") or {kind, record_id}.
export function cites(list) {
  if (!list || !list.length) return null;
  return h("span", { class: "cites" },
    list.map((c) => h("code", { class: "cite", title: typeof c === "string" ? null : c.kind },
      typeof c === "string" ? c : c.record_id)));
}

export function notices(list) {
  if (!list || !list.length) return null;
  return h("ul", { class: "notices" }, list.map((n) => h("li", { class: "notice" }, n)));
}

// Only http(s) URLs become links; anything else stays an inert id.
export function sources(map) {
  const entries = Object.entries(map || {});
  if (!entries.length) return null;
  return h("div", { class: "sources" },
    h("span", { class: "label" }, "Sources"),
    h("ul", null, entries.map(([id, src]) => h("li", null,
      src.url && /^https?:\/\//.test(src.url)
        ? h("a", { href: src.url, rel: "noopener noreferrer nofollow", target: "_blank", title: id },
          src.title || src.url)
        : h("code", null, id),
      src.kind ? h("span", { class: "muted" }, " · " + src.kind.replace(/_/g, " ")) : null))));
}

export function empty(title, text, action) {
  return h("div", { class: "empty" }, h("p", { class: "empty-title" }, title), text ? h("p", null, text) : null,
    action || null);
}

export function loading(text = "Loading…") {
  return h("p", { class: "loading", role: "status" }, text);
}

export function failure(error, retry) {
  return h("div", { class: "error-box", role: "alert" },
    h("p", null, error.message || String(error)),
    retry ? h("button", { class: "ghost", type: "button", onclick: retry }, "Try again") : null);
}

export function pageHead(title, subtitle, ...actions) {
  return h("header", { class: "page-head" },
    h("div", null, h("h1", null, title), subtitle ? h("p", { class: "sub" }, subtitle) : null),
    actions.length ? h("div", { class: "actions" }, actions) : null);
}

export function tabs(options, current, onPick) {
  return h("div", { class: "tabs", role: "tablist" }, options.map(([value, label]) =>
    h("button", {
      type: "button", role: "tab", class: value === current ? "tab active" : "tab",
      "aria-selected": value === current ? "true" : "false", onclick: () => onPick(value),
    }, label)));
}

// ── toasts and confirmation ─────────────────────────────────────────────

export function toast(text, tone = "info") {
  const host = document.getElementById("toasts");
  const node = h("div", { class: `toast ${tone}`, role: "status" }, text);
  host.appendChild(node);
  setTimeout(() => node.remove(), 5000);
}

// Resolves true when the user confirms. body is a node (or nodes) explaining exactly what happens.
export function confirm(title, body, confirmLabel = "Confirm", tone = "primary") {
  const dialog = document.getElementById("confirm");
  clear(dialog,
    h("form", { method: "dialog", class: "dialog" },
      h("h2", null, title),
      h("div", { class: "dialog-body" }, body),
      h("div", { class: "dialog-actions" },
        h("button", { class: "ghost", value: "cancel", type: "submit" }, "Cancel"),
        h("button", { class: tone, value: "ok", type: "submit" }, confirmLabel))));
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}
