// Accounts and access: sign-in, registration, password reset, requesting GPU access, and
// the review of those requests by the named Professor and an admin (both must approve).
import { api, token, h, clear, when, empty, loading, pageHead, tabs, toast, confirm } from "/app/steward/lib.js";
import { field, select, formStatus, status, stamp, table } from "./ui.js";

const ROLES = [["STUDENT", "Student"], ["RESEARCHER", "Researcher"], ["PROFESSOR", "Professor"], ["ADMIN", "Admin"]];

// ── signing in, registering, resetting a password ───────────────────────

export function authScreen(host, { message, mode = "signin", signedIn }) {
  host.hidden = false;
  const show = (next, text) => authScreen(host, { message: text, mode: next, signedIn });
  const note = formStatus();
  note.textContent = message || "";
  const brand = h("div", { class: "brand big" }, h("span", { class: "mark", "aria-hidden": "true" }), h("span", null, h("strong", null, "Prayog")));

  const submit = (work) => async (event) => {
    event.preventDefault();
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    note.textContent = "";
    try { await work(event.target.elements); } catch (e) { note.textContent = e.message; } finally { button.disabled = false; }
  };
  const login = async (email, password, role) => {
    const body = await api("/auth/login", { method: "POST", body: { email, password, role } });
    token.set(body.access_token);
    await signedIn();
  };
  const link = (text, next) => h("button", { type: "button", class: "link", onclick: () => show(next) }, text);
  const password = (name, label, autocomplete) => field(label, h("input", { name, type: "password", autocomplete, minlength: "8", required: true }));

  const forms = {
    signin: () => h("form", {
      class: "signin-card", onsubmit: submit(async (f) => {
        try {
          await login(f.email.value.trim(), f.password.value, f.role.value);
        } catch (e) {
          // Wrong password and wrong role get the same answer, by design (no account probing).
          throw e.status === 401 ? new Error("That email, password and role do not match an account.") : e;
        }
      }),
    }, brand, h("p", { class: "muted" }, "GPU scheduling and experiment records for IIT Kanpur."),
    field("Email or user ID", h("input", { name: "email", autocomplete: "username", required: true })),
    field("Role", select("role", ROLES, "STUDENT")),
    field("Password", h("input", { name: "password", type: "password", autocomplete: "current-password", required: true })),
    h("button", { type: "submit", class: "primary wide" }, "Sign in"), note,
    h("p", { class: "auth-links" }, link("Create an account", "register"), link("Forgot password?", "forgot"))),

    register: () => h("form", {
      class: "signin-card", onsubmit: submit(async (f) => {
        await api("/auth/register", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value, role: f.role.value } });
        history.replaceState(null, "", "/app/#/access");
        await login(f.email.value.trim(), f.password.value, f.role.value);
      }),
    }, brand, h("p", { class: "muted" }, "Students and researchers register here, then request GPU access. Professors are added by an admin."),
    field("Email", h("input", { name: "email", type: "email", autocomplete: "email", required: true })),
    field("I am a", select("role", ROLES.slice(0, 2), "STUDENT")),
    password("password", "Password (at least 8 characters)", "new-password"),
    h("button", { type: "submit", class: "primary wide" }, "Create account"), note,
    h("p", { class: "auth-links" }, link("Back to sign in", "signin"))),

    forgot: () => h("form", {
      class: "signin-card", onsubmit: submit(async (f) => {
        const email = f.email.value.trim();
        const sent = await api("/auth/forgot-password", {
          method: "POST", body: { email, new_password: f.password.value, confirm_password: f.again.value },
        });
        authScreen(host, { message: sent.message, mode: "otp", signedIn, email });
      }),
    }, brand, h("p", { class: "muted" }, "Choose a new password. We email a 6-digit code to confirm it is you."),
    field("Email", h("input", { name: "email", type: "email", autocomplete: "email", required: true })),
    password("password", "New password", "new-password"), password("again", "New password again", "new-password"),
    h("button", { type: "submit", class: "primary wide" }, "Email me a code"), note,
    h("p", { class: "auth-links" }, link("Back to sign in", "signin"))),

    otp: () => h("form", {
      class: "signin-card", onsubmit: submit(async (f) => {
        const done = await api("/auth/verify-otp", { method: "POST", body: { email: f.email.value.trim(), otp: f.otp.value.trim() } });
        show("signin", done.message);
      }),
    }, brand, h("p", { class: "muted" }, "Enter the code from the email to change your password."),
    field("Email", h("input", { name: "email", type: "email", autocomplete: "email", required: true })),
    field("6-digit code", h("input", { name: "otp", inputmode: "numeric", pattern: "\\d{6}", autocomplete: "one-time-code", required: true })),
    h("button", { type: "submit", class: "primary wide" }, "Change password"), note,
    h("p", { class: "auth-links" }, link("Back to sign in", "signin"))),
  };
  clear(host, forms[mode]());
  host.querySelector("input")?.focus();
}

// ── requesting GPU access ───────────────────────────────────────────────

export async function accessPage(ctx) {
  const head = pageHead("GPU access", "Your request needs approval from your Professor and from an admin before you can submit jobs.");
  clear(ctx.view, head, loading());
  const [mine, professors] = await Promise.all([api("/access-requests/me"), api("/professors")]);
  // A rejected or revoked request is history: the user can send a new one. Only a pending or approved one blocks the form.
  const open = mine.find((r) => r.final_status !== "REVOKED" && r.final_status !== "REJECTED");
  const rejected = !open && mine.length && mine[0].final_status === "REJECTED";
  clear(ctx.view, head,
    mine.length ? mine.map(requestCard) : null,
    rejected ? h("p", { class: "muted" }, "Your last request was rejected. You can send a new one below.") : null,
    open ? null : requestForm(ctx, professors));
}

function requestCard(r) {
  return h("article", { class: "card" },
    h("div", { class: "card-head" }, status(r.final_status), h("strong", null, `${r.course} · ${r.branch}`),
      h("span", { class: "muted" }, when(r.created_at))),
    h("dl", { class: "kv" },
      h("dt", null, "Professor's decision"), h("dd", null, status(r.status_professor)),
      h("dt", null, "Admin's decision"), h("dd", null, status(r.status_admin)),
      h("dt", null, "Purpose"), h("dd", null, r.purpose === "COURSE_PROJECT" ? "Course project" : "Research project"),
      h("dt", null, "From"), h("dd", null, stamp(r.start_date)),
      r.end_date ? [h("dt", null, "Until"), h("dd", null, stamp(r.end_date))] : null,
      r.revoked_at ? [h("dt", null, "Ended"), h("dd", null, `${stamp(r.revoked_at)}${r.revoke_reason ? ` · ${r.revoke_reason}` : ""}`)] : null));
}

function requestForm(ctx, professors) {
  const student = ctx.role === "STUDENT";
  const note = formStatus();
  const today = new Date().toISOString().slice(0, 10);
  const form = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      note.textContent = "Sending…";
      try {
        await api("/access-requests", {
          method: "POST",
          body: {
            name: f.name.value, role: ctx.role, course: f.course.value, branch: f.branch.value,
            purpose: student ? f.purpose.value : "RESEARCH_PROJECT", professor_email: f.professor.value,
            roll_number: f.roll.value, start_date: `${f.start.value}T00:00:00Z`,
            end_date: student ? `${f.end.value}T00:00:00Z` : null,
          },
        });
        toast("Request sent to your Professor and the admins.", "good");
        accessPage(ctx);
      } catch (e) {
        note.textContent = e.message;
      }
    },
  },
  field("Full name", h("input", { name: "name", autocomplete: "name", required: true })),
  field("Roll number", h("input", { name: "roll", required: true })),
  field("Course", h("input", { name: "course", required: true })),
  field("Branch", h("input", { name: "branch", required: true })),
  field("Responsible Professor", select("professor", professors.map((p) => [p.email, `${p.name} (${p.department})`]))),
  student ? field("Purpose", select("purpose", [["COURSE_PROJECT", "Course project"], ["RESEARCH_PROJECT", "Research project"]], "COURSE_PROJECT")) : null,
  field("Start date", h("input", { name: "start", type: "date", value: today, required: true })),
  student ? field("End date", h("input", { name: "end", type: "date", required: true })) : null,
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Request access"), note));
  return h("section", { class: "card" }, h("h2", null, "Request GPU access"), form);
}

// ── reviewing requests ──────────────────────────────────────────────────

export async function reviewPage(ctx, state = "PENDING") {
  const admin = ctx.role === "ADMIN";
  const head = pageHead("Access requests", admin
    ? "Every request. Access starts once both the Professor and an admin approve."
    : "Requests that name you as responsible Professor. Access starts once you and an admin both approve.");
  const pick = (s) => reviewPage(ctx, s);
  const bar = tabs([["PENDING", "Pending"], ["APPROVED", "Approved"], ["REJECTED", "Rejected"], ["REVOKED", "Revoked"]], state, pick);
  clear(ctx.view, head, bar, loading());
  const rows = await api(`/access-requests?status=${state}`);
  clear(ctx.view, head, bar, rows.length ? rows.map((r) => reviewCard(ctx, r, () => reviewPage(ctx, state)))
    : empty(state === "PENDING" ? "Nothing to review" : `No ${state.toLowerCase()} requests`));
}

function reviewCard(ctx, r, refresh) {
  const admin = ctx.role === "ADMIN";
  const decide = async (decision) => {
    try {
      await api(`/access-requests/${r.id}/decision`, { method: "POST", body: { status: decision } });
      toast(decision === "APPROVED" ? "Approved." : "Rejected.", decision === "APPROVED" ? "good" : "info");
    } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  const revoke = async () => {
    const ok = await confirm("Revoke this access?", h("p", null, `${r.name} loses GPU access and must submit a new request to get it back.`), "Revoke access", "danger");
    if (!ok) return;
    try { await api(`/access-requests/${r.id}/revoke`, { method: "POST" }); toast("Access revoked."); } catch (e) { toast(e.message, "bad"); }
    refresh();
  };
  const locked = r.final_status === "APPROVED" || r.final_status === "REVOKED";
  return h("article", { class: "card" },
    h("div", { class: "card-head" }, status(r.final_status), h("strong", null, r.name), h("span", null, r.role === "STUDENT" ? "Student" : "Researcher"),
      h("span", { class: "muted" }, when(r.created_at))),
    h("dl", { class: "kv" },
      h("dt", null, "Course"), h("dd", null, `${r.course} · ${r.branch}`),
      h("dt", null, "Roll number"), h("dd", null, r.roll_number),
      h("dt", null, "Purpose"), h("dd", null, r.purpose === "COURSE_PROJECT" ? "Course project" : "Research project"),
      h("dt", null, "Period"), h("dd", null, `${stamp(r.start_date)}${r.end_date ? ` to ${stamp(r.end_date)}` : " onward"}`),
      h("dt", null, "Professor's decision"), h("dd", null, status(r.status_professor)),
      h("dt", null, "Admin's decision"), h("dd", null, status(r.status_admin))),
    h("div", { class: "actions end" },
      locked ? null : [h("button", { type: "button", class: "ghost", onclick: () => decide("REJECTED") }, "Reject"),
        h("button", { type: "button", class: "primary", onclick: () => decide("APPROVED") }, "Approve")],
      admin && r.final_status === "APPROVED" ? h("button", { type: "button", class: "danger", onclick: revoke }, "Revoke…") : null));
}

// ── professors (admin) ──────────────────────────────────────────────────

export async function professorsPage(ctx) {
  const head = pageHead("Professors", "Professor accounts are created here. Students and researchers name them on access requests.");
  clear(ctx.view, head, loading());
  const list = await api("/professors");
  const note = formStatus();
  const form = h("form", {
    class: "grid", onsubmit: async (event) => {
      event.preventDefault();
      const f = event.target.elements;
      note.textContent = "Creating…";
      try {
        const made = await api("/professors", {
          method: "POST",
          body: { name: f.name.value, email: f.email.value.trim(), department: f.department.value },
        });
        await professorsPage(ctx);
        toast(`${made.name} added. They set a password through “Forgot password?”.`, "good");
      } catch (e) {
        note.textContent = e.message;
      }
    },
  },
  field("Name", h("input", { name: "name", required: true })),
  field("Email", h("input", { name: "email", type: "email", required: true })),
  field("Department", h("input", { name: "department", required: true })),
  h("div", { class: "form-foot" }, h("button", { type: "submit", class: "primary" }, "Add Professor"), note));
  clear(ctx.view, head,
    list.length ? table(["Name", "Email", "Department", "Lab"], list.map((p) => [p.name, p.email, p.department, p.lab_id ?? "–"])) : empty("No Professors yet"),
    h("details", { class: "panel collapsible" }, h("summary", null, "Add a Professor"), form));
}
