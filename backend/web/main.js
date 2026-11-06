// The Prayog web app: one sign-in and one sidebar for every role. Each role gets its own
// workspace (FR-INT-05): researcher dashboard, Professor workspace or admin dashboard. The
// Steward's screens join them when the Steward is running. The server checks every request.
import { api, token, h, clear, failure } from "/app/steward/lib.js";
import { overview as stewardOverview, diagnosesList, runDetail as diagnosisDetail, proposalsPage, rebuildsPage, advicePage, knowledgePage } from "/app/steward/owner.js";
import { chatPage } from "/app/steward/chat.js";
import { warningsPage, warningDetail, healthPage } from "/app/steward/admin.js";
import { authScreen, accessPage, reviewPage, professorsPage } from "./accounts.js";
import { dashboard, jobsPage, jobDetail, projectsPage, projectDetail, gpuRunDetail } from "./research.js";
import { workspacePage, studentProject, studentRun, handoversPage } from "./professor.js";
import { poolPage, nodesPage, nodeDetail, maintenancePage, ledgerPage } from "./pool.js";

const $ = (id) => document.getElementById(id);
const OWNER = ["STUDENT", "RESEARCHER"];
const PROF = ["PROFESSOR"];
const ADMIN = ["ADMIN"];
const ALL = [...OWNER, ...PROF, ...ADMIN];

// [heading, [{href, label, roles, steward}]]; steward items show only when it is running.
const NAV = [
  [null, [
    { href: "#/", label: "Dashboard", roles: OWNER },
    { href: "#/jobs", label: "Jobs", roles: OWNER },
    { href: "#/projects", label: "Projects", roles: OWNER },
    { href: "#/access", label: "GPU access", roles: OWNER },
    { href: "#/workspace", label: "Students", roles: PROF },
    { href: "#/handovers", label: "Handovers", roles: PROF },
    { href: "#/pool", label: "Pool", roles: ADMIN },
    { href: "#/nodes", label: "Nodes", roles: ADMIN },
    { href: "#/maintenance", label: "Maintenance", roles: ADMIN },
    { href: "#/ledger", label: "Ledger overview", roles: ADMIN },
    { href: "#/review", label: "Access requests", roles: [...PROF, ...ADMIN] },
    { href: "#/professors", label: "Professors", roles: ADMIN },
  ]],
  ["Steward", [
    { href: "#/steward", label: "Overview", roles: OWNER, steward: true },
    { href: "#/diagnoses", label: "Diagnoses", roles: OWNER, steward: true },
    { href: "#/diagnoses", label: "Student diagnoses", roles: PROF, steward: true },
    { href: "#/proposals", label: "Proposals", roles: OWNER, steward: true },
    { href: "#/rebuilds", label: "Rebuilds", roles: OWNER, steward: true },
    { href: "#/chat", label: "Chat", roles: OWNER, steward: true },
    { href: "#/admin/warnings", label: "Hardware warnings", roles: ADMIN, steward: true },
    { href: "#/admin/health", label: "Health", roles: ADMIN, steward: true },
    { href: "#/knowledge", label: "Reference papers", roles: ALL, steward: true },
  ]],
];

// [pattern, view, roles, needs the Steward]
const ROUTES = [
  [/^#\/?$/, dashboard, OWNER],
  [/^#\/jobs$/, jobsPage, OWNER],
  [/^#\/jobs\/(\d+)$/, jobDetail, OWNER],
  [/^#\/projects$/, projectsPage, OWNER],
  [/^#\/projects\/([^/]+)$/, projectDetail, OWNER],
  [/^#\/projects\/([^/]+)\/runs\/([^/]+)\/(\d+)$/, gpuRunDetail, OWNER],
  [/^#\/access$/, accessPage, OWNER],
  [/^#\/review$/, reviewPage, [...PROF, ...ADMIN]],
  [/^#\/professors$/, professorsPage, ADMIN],
  [/^#\/workspace$/, workspacePage, PROF],
  [/^#\/workspace\/projects\/([^/]+)$/, studentProject, PROF],
  [/^#\/workspace\/runs\/(\d+)$/, studentRun, PROF],
  [/^#\/handovers$/, handoversPage, PROF],
  [/^#\/pool$/, poolPage, ADMIN],
  [/^#\/nodes$/, nodesPage, ADMIN],
  [/^#\/nodes\/(\d+)$/, nodeDetail, ADMIN],
  [/^#\/maintenance$/, maintenancePage, ADMIN],
  [/^#\/ledger$/, ledgerPage, ADMIN],
  [/^#\/steward$/, stewardOverview, OWNER, true],
  [/^#\/diagnoses$/, diagnosesList, [...OWNER, ...PROF], true],
  [/^#\/runs\/([^/]+)$/, (c, id) => diagnosisDetail(c, decodeURIComponent(id)), [...OWNER, ...PROF], true],
  [/^#\/proposals$/, proposalsPage, OWNER, true],
  [/^#\/rebuilds$/, rebuildsPage, OWNER, true],
  [/^#\/rebuilds\/(\d+)\/advice$/, advicePage, OWNER, true],
  [/^#\/chat$/, chatPage, OWNER, true],
  [/^#\/knowledge$/, knowledgePage, ALL, true],
  [/^#\/admin\/warnings$/, warningsPage, ADMIN, true],
  [/^#\/admin\/warnings\/([0-9a-f-]{36})$/, warningDetail, ADMIN, true],
  [/^#\/admin\/health$/, healthPage, ADMIN, true],
];

const HOME = { PROFESSOR: "#/workspace", ADMIN: "#/pool" };
const ROLE_LABEL = { STUDENT: "Student", RESEARCHER: "Researcher", PROFESSOR: "Professor", ADMIN: "Admin" };

let user = null;

const ctx = {
  get user() { return user; },
  get role() { return user && user.role; },
  get steward() { return stewardOn; },
  view: $("view"),
  go(hash) { if (location.hash === hash) route(); else location.hash = hash; },
};
let stewardOn = false;

async function whoAmI() {
  try { return await api("/auth/me"); } catch (e) { if (e.status === 401) return null; throw e; }
}

function showAuth(message, mode) {
  $("app").hidden = true;
  authScreen($("signin-screen"), { message, mode, signedIn: start });
}

async function signOut() {
  if (stewardOn) { try { await api("/steward/s3/logout", { method: "POST" }); } catch (_) { /* no session */ } }
  try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* already expired */ }
  token.clear();
  user = null;
  history.replaceState(null, "", "/app/");
  showAuth("You are signed out.");
}

function renderShell() {
  $("signin-screen").hidden = true;
  $("app").hidden = false;
  clear($("nav"), NAV.map(([heading, items]) => {
    const shown = items.filter((n) => n.roles.includes(user.role) && (!n.steward || stewardOn));
    if (!shown.length) return null;
    return [heading ? h("p", { class: "nav-heading" }, heading) : null,
      shown.map((n) => h("a", { href: n.href, class: "nav-item", "data-href": n.href }, h("span", null, n.label)))];
  }));
  clear($("who"), h("span", { class: "who-email", title: user.email }, user.email),
    h("span", { class: "role-chip" }, ROLE_LABEL[user.role] || user.role));
}

// The page a hash belongs to in the sidebar.
const SECTION = [[/^#\/runs\//, "#/diagnoses"], [/^#\/jobs\//, "#/jobs"], [/^#\/projects\//, "#/projects"], [/^#\/rebuilds\//, "#/rebuilds"],
  [/^#\/workspace\//, "#/workspace"], [/^#\/nodes\//, "#/nodes"], [/^#\/admin\/warnings\//, "#/admin/warnings"]];

function markNav() {
  let hash = location.hash || "#/";
  if (hash === "#") hash = "#/";
  for (const [pattern, section] of SECTION) if (pattern.test(hash)) hash = section;
  for (const a of document.querySelectorAll(".nav-item")) {
    const active = a.dataset.href === hash;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

async function route() {
  if (!user) return;
  const hash = location.hash || "#/";
  if ((hash === "#/" || hash === "#") && HOME[user.role]) { location.replace(HOME[user.role]); return; }
  markNav();
  for (const [pattern, view, roles, needsSteward] of ROUTES) {
    const match = hash.match(pattern);
    if (!match) continue;
    if (!roles.includes(user.role) || (needsSteward && !stewardOn)) break;
    try {
      await view(ctx, ...match.slice(1));
    } catch (e) {
      if (e.status === 401) { token.clear(); user = null; showAuth("Your sign-in expired. Please sign in again."); return; }
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

async function start() {
  user = await whoAmI();
  if (!user) { showAuth(); return; }
  stewardOn = await fetch("/steward/healthz").then((r) => r.ok).catch(() => false);
  renderShell();
  await route();
}

start().catch((e) => {
  $("app").hidden = false;
  clear(ctx.view, failure(e, () => location.reload()));
});
