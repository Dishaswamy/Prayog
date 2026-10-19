# Prayog API: Backend Framework, Authentication & Authorisation

- **Base URL (local):** `http://127.0.0.1:8000`
- **Interactive docs:** `/docs` (Swagger UI), `/redoc`
- **Format:** JSON request and response bodies (`Content-Type: application/json`)

> **Scope:** this documents the endpoints of **my part** of the backend: the framework's health check, **authentication** (`/auth/*`) and **authorisation** (`/professors`, `/access-requests/*`). Endpoints for GPU scheduling, MLflow and the AI modules are documented separately by their owners. Those modules use the same [conventions](#conventions) (Bearer token, error format, status codes) defined here.

For architecture, setup and the reasons behind the rules, see [README.md](README.md).


---

## Contents

- [Conventions](#conventions)
- [Health (framework)](#health-framework)
  - [GET /health](#get-health)
- [Auth (authentication)](#auth-authentication)
  - [POST /auth/register](#post-authregister)
  - [POST /auth/login](#post-authlogin)
  - [POST /auth/cli/login](#post-authclilogin)
  - [POST /auth/logout](#post-authlogout)
  - [GET /auth/me](#get-authme)
  - [POST /auth/forgot-password](#post-authforgot-password)
  - [POST /auth/verify-otp](#post-authverify-otp)
- [Professors (authorisation)](#professors-authorisation)
  - [GET /professors](#get-professors)
  - [POST /professors](#post-professors)
- [Access requests (authorisation)](#access-requests-authorisation)
  - [POST /access-requests](#post-access-requests)
  - [GET /access-requests/me](#get-access-requestsme)
  - [GET /access-requests](#get-access-requests)
  - [GET /access-requests/{id}](#get-access-requestsid)
  - [POST /access-requests/{id}/decision](#post-access-requestsiddecision)
  - [POST /access-requests/{id}/revoke](#post-access-requestsidrevoke)
- [Objects](#objects)
- [Endpoint summary](#endpoint-summary)

---

## Conventions

### Authentication
Protected endpoints need the token from `/auth/login`:

```
Authorization: Bearer <access_token>
```

Tokens expire after **60 minutes**, and also stop working when the user resets their password or calls [logout](#post-authlogout).

### Enum values
Values are **uppercase**. Request bodies also accept lowercase for `role` and `status` (`"student"` becomes `"STUDENT"`).

| Name | Values |
|---|---|
| Role | `STUDENT`, `RESEARCHER`, `PROFESSOR`, `ADMIN` |
| Decision | `APPROVED`, `REJECTED` |
| Decision status | `PENDING`, `APPROVED`, `REJECTED` |
| Final status | `PENDING`, `APPROVED`, `REJECTED`, `REVOKED` |

### Dates
ISO 8601 date-times, for example `"2026-10-15T00:00:00"`. Dates with a timezone (`"...Z"`, `"+05:30"`) are accepted and converted to UTC. All timestamps in responses are UTC.

### Errors
Every error has a `detail` field.

```json
{ "detail": "This roll number is already in use" }
```

Validation errors (**422**) list each problem:
```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "new_password"],
      "msg": "String should have at least 8 characters",
      "input": "dummy",
      "ctx": { "min_length": 8 }
    }
  ]
}
```

| Code | Meaning |
|---|---|
| 200 / 201 | Success / created |
| 400 | Request is valid JSON but breaks a business rule (dates, OTP, …) |
| 401 | Not logged in, token invalid or expired, or wrong login details |
| 403 | Logged in, but your role or ownership doesn't allow this |
| 404 | Not found |
| 409 | Conflicts with existing data (duplicate, request locked, …) |
| 422 | Body or parameters failed validation |
| 503 | Database unreachable (`/health` only) |

---

## Health (framework)

### GET /health
Checks that the API is up and PostgreSQL is reachable.

**Auth:** none

**200**
```json
{ "status": "ok", "database": "ok" }
```
**503**
```json
{ "status": "error", "database": "unreachable" }
```

---

## Auth (authentication)

### POST /auth/register
Creates a student or researcher account.

**Auth:** none

**Body**
| Field | Type | Rules |
|---|---|---|
| `email` | string | valid email, must end in `@iitk.ac.in` |
| `password` | string | 8–128 characters |
| `role` | string | `STUDENT` or `RESEARCHER` |

```json
{ "email": "dishac22@iitk.ac.in", "password": "MyPass1234", "role": "STUDENT" }
```

**201** returns a [User](#user)
```json
{ "user_id": "dishac22", "email": "dishac22@iitk.ac.in", "role": "STUDENT" }
```

| Error | When |
|---|---|
| 400 `Only IITK email addresses are allowed` | email doesn't end in `@iitk.ac.in` |
| 409 `User with this email already exists` | email already registered |
| 409 `User ID already exists` | the part before `@` is already taken |
| 422 | invalid email, password too short, or role is `PROFESSOR`/`ADMIN` |

---

### POST /auth/login
Logs in and returns a JWT.

**Auth:** none

**Body**
| Field | Type | Rules |
|---|---|---|
| `email` | string | registered email **or user ID** (the part before `@`), any case |
| `password` | string | |
| `role` | string | `STUDENT` / `RESEARCHER` / `PROFESSOR` / `ADMIN`; must match the account's role |

```json
{ "email": "dishac22@iitk.ac.in", "password": "MyPass1234", "role": "STUDENT" }
```

**200**
```json
{ "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...", "token_type": "bearer" }
```

| Error | When |
|---|---|
| 401 `Invalid email, password or role` | unknown account, wrong password or wrong role (deliberately the same message) |
| 422 | missing field or invalid role value |

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"dishac22@iitk.ac.in","password":"MyPass1234","role":"STUDENT"}'
```

---

### POST /auth/cli/login
The login used by the `prayog` command line. Same body, same token and same generic `401` as [POST /auth/login](#post-authlogin), but it only succeeds for users who **currently have access**. The web portal keeps using `/auth/login`, because a student has to log in there to request access in the first place.

**Auth:** none

**Body:** same as `/auth/login` (`email`, `password`, `role`)

**200** `{ "access_token": "...", "token_type": "bearer" }`

| Error | When |
|---|---|
| 401 `Invalid email, password or role` | unknown account, wrong password or wrong role (the same message as `/auth/login`; it says nothing about access) |
| 403 `code: INSUFFICIENT_PERMISSION` | the password was right but access was never approved: no request, still waiting for the professor and/or admin, or rejected (the `detail` says which) |
| 403 `code: ACCESS_EXPIRED` | a student's approved period has ended (checked against server time, even before the automatic revoke runs) |
| 403 `code: ACCESS_REVOKED` | an admin revoked the access |

Students and researchers need an approved access record; professors need a professor profile; admins always pass. The check happens **at login only**: a token already issued keeps working until it expires (60 minutes) or is logged out, even if access is revoked later.

---

### POST /auth/logout
Ends **this login**: the token in the `Authorization` header stops working at once, on every protected endpoint. The user's other logins (another browser, the CLI) are not affected. Used by `prayog logout` and available to the web portal.

The server remembers the token only until it would have expired anyway, then forgets it.

**For the web portal (frontend):** the "Log out" button must call this endpoint with the user's token **first**, and only then delete the token it stored in the browser. Deleting the stored token alone only hides it: the token stays valid on the server until it expires (60 minutes), so a copy of it (shared computer, leaked log) would still work. Clear the stored token even if the call fails (for example, offline), so the user always appears logged out. A `401` reply means the token is already dead, which is fine.

```js
async function logout() {
  try {
    await fetch(`${API}/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
  } finally {
    localStorage.removeItem("token");   // wherever the portal keeps it
  }
}
```

The terminal (`prayog logout`) and the browser have separate tokens, so logging out in one does not log out the other.

**Auth:** any logged-in user

**Body:** none

**200**
```json
{ "message": "Logged out." }
```

| Error | When |
|---|---|
| 401 `Logged out, please log in again` | this token was already logged out (so a second logout also gets this) |
| 401 / 403 | missing, invalid or expired token |

---

### GET /auth/me
Returns the logged-in user.

**Auth:** any logged-in user

**200** returns a [User](#user)
```json
{ "user_id": "dishac22", "email": "dishac22@iitk.ac.in", "role": "STUDENT" }
```

| Error | When |
|---|---|
| 401 `Invalid authentication token` | missing, malformed or tampered token |
| 401 `Token has expired` | more than 60 minutes after login |
| 401 `Session expired, please log in again` | the password was reset after this token was issued |
| 401 `Logged out, please log in again` | the token was ended with `POST /auth/logout` |
| 401 `User not found` | the account no longer exists |

These 401 errors apply to **every** protected endpoint.

---

### POST /auth/forgot-password
**Step 1 of password reset.** Takes the registered email and the new password, and emails a 6-digit OTP. The password is **not** changed yet.

New professors use this to set their first password.

**Auth:** none

**Body**
| Field | Type | Rules |
|---|---|---|
| `email` | string | registered email |
| `new_password` | string | 8–128 characters |
| `confirm_password` | string, optional | the password typed a second time; if sent, it must equal `new_password` |

```json
{ "email": "dishac22@iitk.ac.in", "new_password": "NewPass5678", "confirm_password": "NewPass5678" }
```

**200**, always the same, whether or not the email exists:
```json
{ "message": "If an account with that email exists, an OTP has been sent to it." }
```

**Behaviour**
- The OTP is valid for **10 minutes** and works **once**.
- Requesting a new OTP cancels the previous one.
- At most **one OTP per minute** per user. Extra requests still get 200, but no email is sent.
- In development (`SMTP_HOST` empty), the email is printed in the server terminal.

| Error | When |
|---|---|
| 422 | invalid email, password shorter than 8 characters, or `confirm_password` differs |

---

### POST /auth/verify-otp
**Step 2 of password reset.** If the OTP is correct, the password from step 1 takes effect and all existing sessions of that user are logged out.

**Auth:** none

**Body**
| Field | Type | Rules |
|---|---|---|
| `email` | string | same email as step 1 |
| `otp` | string | exactly 6 digits |

```json
{ "email": "dishac22@iitk.ac.in", "otp": "964227" }
```

**200**
```json
{ "message": "Password changed. Please log in with your new password." }
```

| Error | When |
|---|---|
| 400 `Invalid or expired OTP` | wrong OTP, expired, already used, replaced by a newer one, or unknown email |
| 400 `Too many wrong attempts. Please request a new OTP.` | 5th wrong attempt; the OTP is now cancelled |
| 422 | OTP isn't exactly 6 digits |

---

## Professors (authorisation)

### GET /professors
Lists all professors, for example to fill the "choose professor" dropdown on the request form.

**Auth:** any logged-in user

**200** returns an array of [Professor](#professor), sorted by name
```json
[
  { "id": 1, "name": "Prof A", "email": "profa@iitk.ac.in", "department": "AE" }
]
```

---

### POST /professors
Creates a professor: the `PROFESSOR` login account and the professor profile, in one transaction.

If you leave out `password`, the server generates one and returns it **once**, in `temporary_password`. Give it to the professor: they log in with it directly (role `PROFESSOR`) and can change it later with [forgot-password](#post-authforgot-password). It is not stored in readable form, so it cannot be shown again.

**Auth:** `ADMIN`

**Body**
| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–100 characters |
| `email` | string | `@iitk.ac.in` |
| `department` | string | 1–100 characters |
| `lab_id` | integer, optional | the lab this professor runs; research students and researchers they approve are scheduled under it |
| `password` | string, optional, 8–128 characters | leave it out (recommended) and the server generates one and returns it once as `temporary_password` |

```json
{ "name": "Prof A", "email": "profa@iitk.ac.in", "department": "AE", "lab_id": 1 }
```

**201** returns a [Professor](#professor) plus `temporary_password` (only when the server generated one; shown once)
```json
{ "id": 1, "name": "Prof A", "email": "profa@iitk.ac.in", "department": "AE", "lab_id": 1, "temporary_password": "k3Jx9...generated" }
```

| Error | When |
|---|---|
| 400 `Only IITK email addresses are allowed` | non-IITK email |
| 403 `Requires role: ADMIN` | caller isn't an admin |
| 409 `A professor with this email already exists` | duplicate professor |
| 409 `User with this email already exists` | the email already belongs to another account |

---

## Access requests (authorisation)

### POST /access-requests
Submits an access request.

**Auth:** `STUDENT` or `RESEARCHER`

**Body**
| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–100 characters |
| `role` | string | `STUDENT` / `RESEARCHER`; must match your registered role |
| `course` | string | 1–100 characters |
| `branch` | string | 1–100 characters |
| `purpose` | string | `COURSE_PROJECT` (teaching pool) / `RESEARCH_PROJECT` (lab GPUs). **Students:** required. **Researchers:** defaults to `RESEARCH_PROJECT`; `COURSE_PROJECT` is refused |
| `professor_email` | string | email of an existing professor |
| `roll_number` | string | 1–50 characters, required for both roles |
| `start_date` | date-time | required |
| `end_date` | date-time or null | **students:** required, after `start_date`. **Researchers:** ignored (stored as null) |

```json
{
  "name": "Disha",
  "role": "STUDENT",
  "course": "BTech",
  "branch": "AE",
  "purpose": "COURSE_PROJECT",
  "professor_email": "profa@iitk.ac.in",
  "roll_number": "220345",
  "start_date": "2026-10-15T00:00:00",
  "end_date": "2027-05-01T00:00:00"
}
```

**201** returns an [AccessRequest](#accessrequest) with all statuses `PENDING`

| Error | When |
|---|---|
| 400 `Purpose is required for students…` | student without `purpose` |
| 400 `Researchers can only request RESEARCH_PROJECT access` | researcher sent `COURSE_PROJECT` |
| 400 `End date is required for students` | student without `end_date` |
| 400 `End date must be after start date` | |
| 403 `Requires role: STUDENT, RESEARCHER` | caller is a professor or admin |
| 403 `Your registered role is X, but you selected Y` | role mismatch |
| 404 `Professor with the given email was not found` | |
| 409 `You already have an access request (status: …)` | you already have a request that is `PENDING`, `REJECTED` or `APPROVED` |
| 409 `This roll number is already in use` | the roll number belongs to an approved user or another open request |

---

### GET /access-requests/me
Lists the caller's own requests, newest first.

**Auth:** any logged-in user

**200** returns an array of [AccessRequest](#accessrequest)

---

### GET /access-requests
Lists requests for review, newest first.
- **Professor:** only requests addressed to them.
- **Admin:** all requests.

**Auth:** `PROFESSOR` or `ADMIN`

**Query parameters**
| Name | Type | Description |
|---|---|---|
| `status` | string, optional | filter by final status: `PENDING`, `APPROVED`, `REJECTED` or `REVOKED` (any case) |

```
GET /access-requests?status=PENDING
```

**200** returns an array of [AccessRequest](#accessrequest)

| Error | When |
|---|---|
| 400 `status must be one of [...]` | invalid status filter |
| 403 `Requires role: PROFESSOR, ADMIN` | |
| 404 `Professor profile not found for this account` | professor account without a profile |

---

### GET /access-requests/{id}
Gets one request.

**Auth:** the applicant, the professor it's addressed to, or any admin

**200** returns an [AccessRequest](#accessrequest)

| Error | When |
|---|---|
| 403 `You are not allowed to view this request` | |
| 404 `Access request not found` | |

---

### POST /access-requests/{id}/decision
Approves or rejects a request. The professor sets `status_professor` and an admin sets `status_admin`. After every decision, `final_status` is recalculated:

| Professor | Admin | `final_status` |
|---|---|---|
| either `REJECTED` | | `REJECTED` (can still be changed) |
| `APPROVED` | `APPROVED` | `APPROVED`: user added to `students`/`researchers`; request **locked** |
| otherwise | | `PENDING` |

**Auth:** `PROFESSOR` (only for requests addressed to them) or `ADMIN`

**Body**
| Field | Type | Values |
|---|---|---|
| `status` | string | `APPROVED` or `REJECTED` |

```json
{ "status": "APPROVED" }
```

**200** returns the updated [AccessRequest](#accessrequest)
```json
{
  "id": 1,
  "status_professor": "APPROVED",
  "status_admin": "APPROVED",
  "final_status": "APPROVED",
  "...": "..."
}
```

| Error | When |
|---|---|
| 403 `Requires role: PROFESSOR, ADMIN` | |
| 403 `This request is not addressed to you` | different professor |
| 404 `Access request not found` | |
| 409 `This request is already approved and locked. Only an admin can revoke access.` | |
| 409 `This access has been revoked. The user must submit a new request.` | |
| 409 `Could not finalize: this user, email or roll number already exists in the approved records` | duplicate found at the moment of approval |
| 422 | `status` isn't `APPROVED`/`REJECTED` |

```bash
curl -X POST http://127.0.0.1:8000/access-requests/1/decision \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"APPROVED"}'
```

---

### POST /access-requests/{id}/revoke
Removes approved access. Deletes the user's `students`/`researchers` row and sets `final_status` to `REVOKED`. The user can then submit a new request.

**Auth:** `ADMIN`

**Body:** none

**200** returns the updated [AccessRequest](#accessrequest) with `"final_status": "REVOKED"`

| Error | When |
|---|---|
| 403 `Requires role: ADMIN` | |
| 404 `Access request not found` | |
| 409 `Only approved access can be revoked` | request isn't `APPROVED` |

---

## Objects

### User
| Field | Type | Description |
|---|---|---|
| `user_id` | string | public id, the part of the email before `@` |
| `email` | string | lowercase |
| `role` | string | `STUDENT` / `RESEARCHER` / `PROFESSOR` / `ADMIN` |

### Professor
| Field | Type |
|---|---|
| `id` | integer, the professor's id (used in `AccessRequest.professor_id`) |
| `name` | string |
| `email` | string |
| `department` | string |
| `lab_id` | integer or null, the lab this professor runs |

### AccessRequest
| Field | Type | Description |
|---|---|---|
| `id` | integer | request id |
| `user_id` | integer | applicant's internal user id |
| `name` | string | |
| `role` | string | `STUDENT` / `RESEARCHER` |
| `course` | string | |
| `branch` | string | |
| `purpose` | string | `COURSE_PROJECT` / `RESEARCH_PROJECT` |
| `professor_id` | integer | the professor the request is addressed to |
| `roll_number` | string | |
| `start_date` | date-time | UTC |
| `end_date` | date-time or null | null for researchers |
| `status_professor` | string | `PENDING` / `APPROVED` / `REJECTED` |
| `status_admin` | string | `PENDING` / `APPROVED` / `REJECTED` |
| `final_status` | string | `PENDING` / `APPROVED` / `REJECTED` / `REVOKED` |
| `revoke_requested_at` | date-time or null | UTC. Set while a revoke is pending: the user still has unfinished GPU jobs, so no new work is accepted and access ends when they finish. `final_status` stays `APPROVED` until then |
| `revoked_at` | date-time or null | UTC, set when `final_status` becomes `REVOKED` |
| `revoke_reason` | string or null | `ADMIN` (removed by an admin) or `EXPIRED` (a student's `end_date` passed; revoked automatically) |
| `created_at` | date-time | UTC |
| `updated_at` | date-time | UTC, changes on every decision |

```json
{
  "id": 1,
  "user_id": 3,
  "name": "Disha",
  "role": "STUDENT",
  "course": "BTech",
  "branch": "AE",
  "purpose": "COURSE_PROJECT",
  "professor_id": 1,
  "roll_number": "220345",
  "start_date": "2026-10-15T00:00:00",
  "end_date": "2027-05-01T00:00:00",
  "status_professor": "PENDING",
  "status_admin": "PENDING",
  "final_status": "PENDING",
  "created_at": "2026-10-14T09:12:03.512Z",
  "updated_at": "2026-10-14T09:12:03.512Z"
}
```

---

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | API and database status |
| POST | `/auth/register` | none | create a student/researcher account |
| POST | `/auth/login` | none | get a JWT (email + password + role) |
| POST | `/auth/cli/login` | none | terminal login: like `/auth/login`, but only for users with current access |
| POST | `/auth/logout` | any | end this login (the token stops working) |
| GET | `/auth/me` | any | current user |
| POST | `/auth/forgot-password` | none | step 1: email + new password → OTP emailed |
| POST | `/auth/verify-otp` | none | step 2: email + OTP → password changed |
| GET | `/professors` | any | list professors |
| POST | `/professors` | ADMIN | add a professor |
| POST | `/access-requests` | STUDENT, RESEARCHER | submit a request |
| GET | `/access-requests/me` | any | my requests |
| GET | `/access-requests` | PROFESSOR, ADMIN | requests to review (`?status=`) |
| GET | `/access-requests/{id}` | owner / their professor / ADMIN | one request |
| POST | `/access-requests/{id}/decision` | PROFESSOR (own), ADMIN | approve / reject |
| POST | `/access-requests/{id}/revoke` | ADMIN | remove approved access |
