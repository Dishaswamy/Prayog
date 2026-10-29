// Steward web UI: sign-in, role-based navigation and the router. Each role sees only the
// screens its permissions allow (SADD §8.3); the server still checks every request.
import { api, token, h, clear, failure, toast } from "./lib.js";
import { overview, diagnosesList, runDetail, proposalsPage, rebuildsPage, advicePage, knowledgePage } from "./owner.js";
import { chatPage } from "./chat.js";
import { warningsPage, warningDetail, healthPage } from "./admin.js";

const $ = (id) => document.getElementById(id);

const OWNER = ["STUDENT", "RESEARCHER"];
const NAV = [
  { href: "#/", label: "Overview", roles: OWNER },
  { href: "#/diagnoses", label: "Diagnoses", roles: OWNER, tag: "S1" },
  { href: "#/diagnoses", label: "Student diagnoses", roles: ["PROFESSOR"], tag: "S1" },
  { href: "#/proposals", label: "Proposals", roles: OWNER },
  { href: "#/rebuilds", label: "Rebuilds", roles: OWNER, tag: "S3 · S4" },
  { href: "#/chat", label: "Steward chat", roles: OWNER, tag: "S3" },
  { href: "#/admin/warnings", label: "Hardware warnings", roles: ["ADMIN"], tag: "S2" },
  { href: "#/admin/health", label: "Steward health", roles: ["ADMIN"] },
  { href: "#/knowledge", label: "Reference papers", roles: [...OWNER, "PROFESSOR", "ADMIN"] },
];

// [pattern, view, roles]
const ROUTES = [
  [/^#\/?$/, (c) => overview(c), OWNER],
  [/^#\/diagnoses$/, (c) => diagnosesList(c), [...OWNER, "PROFESSOR"]],
  [/^#\/runs\/([^/]+)$/, (c, id) => runDetail(c, decodeURIComponent(id)), [...OWNER, "PROFESSOR"]],
  [/^#\/proposals$/, (c) => proposalsPage(c), OWNER],
  [/^#\/rebuilds$/, (c) => rebuildsPage(c), OWNER],
  [/^#\/rebuilds\/(\d+)\/advice$/, (c, id) => advicePage(c, id), OWNER],
  [/^#\/chat$/, (c) => chatPage(c), OWNER],
  [/^#\/knowledge$/, (c) => knowledgePage(c), [...OWNER, "PROFESSOR", "ADMIN"]],
  [/^#\/admin\/warnings$/, (c) => warningsPage(c), ["ADMIN"]],
  [/^#\/admin\/warnings\/([0-9a-f-]{36})$/, (c, id) => warningDetail(c, id), ["ADMIN"]],
  [/^#\/admin\/health$/, (c) => healthPage(c), ["ADMIN"]],
];

const HOME = { PROFESSOR: "#/diagnoses", ADMIN: "#/admin/warnings" };
const ROLE_LABEL = { STUDENT: "Student", RESEARCHER: "Researcher", PROFESSOR: "Professor", ADMIN: "Admin" };

let user = null;

const ctx = {
  get user() { return user; },
  get role() { return user && user.role; },
  view: $("view"),
  go(hash) { if (location.hash === hash) route(); else location.hash = hash; },
};

// ── signing in ──────────────────────────────────────────────────────────

async function whoAmI() {
  try { return await api("/auth/me"); } catch (e) { if (e.status === 401) return null; throw e; }
}

function showSignIn(message) {
  $("app").hidden = true;
  $("signin-screen").hidden = false;
  $("signin-status").textContent = message || "";
  $("email").focus();
}

$("signin").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("signin-status");
  const button = $("signin-button");
  status.textContent = "";
  button.disabled = true;
  try {
    // Wrong password and wrong role get the same answer, by design (no account probing).
    const body = await api("/auth/login", {
      method: "POST",
      body: { email: $("email").value.trim(), password: $("password").value, role: $("role").value },
    });
    token.set(body.access_token);
    $("password").value = "";
    await start();
  } catch (e) {
    status.textContent = e.status === 401 ? "That email, password and role do not match an account." : e.message;
  } finally {
    button.disabled = false;
  }
});

async function signOut() {
  try { await api("/steward/s3/logout", { method: "POST" }); } catch (_) { /* no session */ }
  try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* already expired */ }
  token.clear();
  user = null;
  history.replaceState(null, "", "/steward/ui/");
  showSignIn("You are signed out.");
}

// ── the shell ───────────────────────────────────────────────────────────

function renderShell() {
  $("signin-screen").hidden = true;
  $("app").hidden = false;
  const items = NAV.filter((n) => n.roles.includes(user.role));
  clear($("nav"), items.map((n) => h("a", { href: n.href, class: "nav-item", "data-href": n.href },
    h("span", null, n.label), n.tag ? h("span", { class: "nav-tag" }, n.tag) : null)));
  clear($("who"),
    h("span", { class: "who-email", title: user.email }, user.email),
    h("span", { class: "role-chip" }, ROLE_LABEL[user.role] || user.role));
}

function markNav() {
  const hash = location.hash || "#/";
  for (const a of document.querySelectorAll(".nav-item")) {
    const href = a.dataset.href;
    const active = href === "#/" ? hash === "#/" || hash === "#" || hash === ""
      : hash === href || hash.startsWith(href + "/") || (href === "#/diagnoses" && hash.startsWith("#/runs/"))
        || (href === "#/rebuilds" && hash.startsWith("#/rebuilds/"));
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

async function route() {
  if (!user) return;
  let hash = location.hash || "#/";
  if (hash === "#/" && HOME[user.role]) { location.replace(HOME[user.role]); return; }
  markNav();
  for (const [pattern, view, roles] of ROUTES) {
    const match = hash.match(pattern);
    if (!match) continue;
    if (!roles.includes(user.role)) break;
    try {
      await view(ctx, ...match.slice(1));
    } catch (e) {
      if (e.status === 401) { token.clear(); user = null; showSignIn("Your sign-in expired. Please sign in again."); return; }
      clear(ctx.view, failure(e, route));
    }
    ctx.view.focus({ preventScroll: true });
    return;
  }
  location.replace(HOME[user.role] || "#/");
}

$("signout").addEventListener("click", signOut);
$("menu").addEventListener("click", () => document.body.classList.toggle("nav-open"));
$("nav").addEventListener("click", () => document.body.classList.remove("nav-open"));
window.addEventListener("hashchange", route);

// ── opening a single-use Steward link: /steward/s/<token> ───────────────

async function openLink(linkToken) {
  try {
    await api(`/steward/s/${encodeURIComponent(linkToken)}/open`, { method: "POST" });
    history.replaceState(null, "", "/steward/ui/#/chat");   // the token never stays in the URL
  } catch (e) {
    history.replaceState(null, "", "/steward/ui/#/rebuilds");
    toast(e.message, "bad");
  }
}

async function start() {
  user = await whoAmI();
  const link = location.pathname.match(/^\/steward\/s\/([^/]+)$/);
  if (!user) {
    showSignIn(link ? "Sign in to open this Steward link. Only the owner of the failed rebuild can open it." : "");
    return;
  }
  if (link) await openLink(decodeURIComponent(link[1]));
  renderShell();
  await route();
}

start().catch((e) => {
  $("app").hidden = false;
  clear(ctx.view, failure(e, () => location.reload()));
});
