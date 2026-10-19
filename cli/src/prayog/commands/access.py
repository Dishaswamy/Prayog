"""A student's or researcher's own access: see where it stands (US-STU-03/06).

Requests are submitted in the web portal, not here (UC-A02).
"""
import os

from prayog.context import Context
from prayog.ui import day, table

PORTAL_HINT = "Submit a request in the Prayog web portal."


def portal_message() -> str:
    """Where to go to request access. PRAYOG_PORTAL_URL, if set, is shown as the address."""
    url = os.environ.get("PRAYOG_PORTAL_URL")
    where = f"the Prayog web portal at {url}" if url else "the Prayog web portal"
    return (f"Access requests are not made from the command line. Log in to {where}, "
            "open the access request form, and submit it there.\n"
            "Once it is approved, run `prayog access status` here to check it.")


def describe_state(r: dict) -> str:
    """One plain-words line for an access request, so a denied command is never a mystery."""
    final = r["final_status"]
    if final == "APPROVED":
        if r.get("revoke_requested_at"):
            return "ENDING. Revocation is pending until your unfinished jobs complete; no new work is accepted."
        end = r.get("end_date")
        return f"ACTIVE until {day(end)}" if end else "ACTIVE (no end date)"
    if final == "REVOKED":
        if r.get("revoke_reason") == "EXPIRED":
            return f"EXPIRED on {day(r.get('revoked_at'))}. Submit a new request in the web portal to continue."
        return f"REVOKED on {day(r.get('revoked_at'))} by an admin. Submit a new request in the web portal to continue."
    if final == "REJECTED":
        return "REJECTED. See your request for who rejected it."
    return f"PENDING (professor: {r['status_professor']}, admin: {r['status_admin']})"


def setup_access(p):
    sub = p.add_subparsers(dest="access_cmd", metavar="<subcommand>", required=True)

    r = sub.add_parser("request", help="how to request access (done in the web portal)")
    r.set_defaults(func=request)

    s = sub.add_parser("status", help="show your access requests and whether access is active")
    s.set_defaults(func=status)


def status(ctx: Context, args) -> None:
    mine = ctx.client().get("/access-requests/me")
    if not mine:
        print(f"No access request yet. {PORTAL_HINT}")
        return
    print(table(["ID", "PURPOSE", "PROFESSOR", "ADMIN", "STATE"],
                [[r["id"], r["purpose"], r["status_professor"], r["status_admin"], describe_state(r)]
                 for r in mine]))


def request(ctx: Context, args) -> None:
    print(portal_message())
