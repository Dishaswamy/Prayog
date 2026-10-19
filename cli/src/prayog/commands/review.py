"""Professors and admins: review, decide on and revoke access requests (UC-A03, A04, A09)."""
from prayog.commands.access import describe_state
from prayog.context import Context
from prayog.ui import day, table


def setup_requests(p):
    sub = p.add_subparsers(dest="requests_cmd", metavar="<subcommand>", required=True)

    s = sub.add_parser("list", help="list requests (professors: those addressed to you; admins: all)")
    s.add_argument("--status", type=str.upper, choices=["PENDING", "APPROVED", "REJECTED", "REVOKED"])
    s.set_defaults(func=list_requests)

    s = sub.add_parser("show", help="show one request in full")
    s.add_argument("id", type=int)
    s.set_defaults(func=show)

    for name, helptext in (("approve", "approve a request"), ("reject", "reject a request")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id", type=int)
        s.set_defaults(func=decide, decision="APPROVED" if name == "approve" else "REJECTED")

    s = sub.add_parser("revoke", help="(admin) end an approved user's access")
    s.add_argument("id", type=int)
    s.set_defaults(func=revoke)


def list_requests(ctx: Context, args) -> None:
    rows = ctx.client().get("/access-requests", params={"status": args.status} if args.status else None)
    if not rows:
        print("No requests.")
        return
    print(table(["ID", "NAME", "ROLE", "PURPOSE", "PROFESSOR", "ADMIN", "FINAL", "START", "END"],
                [[r["id"], r["name"], r["role"], r["purpose"], r["status_professor"], r["status_admin"],
                  r["final_status"], day(r["start_date"]), day(r["end_date"])] for r in rows]))


def show(ctx: Context, args) -> None:
    r = ctx.client().get(f"/access-requests/{args.id}")
    for label, value in (("Request", r["id"]), ("Name", r["name"]), ("Role", r["role"]),
                         ("Roll number", r["roll_number"]), ("Course / branch", f"{r['course']} / {r['branch']}"),
                         ("Purpose", r["purpose"]), ("Start", day(r["start_date"])), ("End", day(r["end_date"])),
                         ("Professor", r["status_professor"]), ("Admin", r["status_admin"]),
                         ("State", describe_state(r))):
        print(f"{label + ':':<18}{value}")


def decide(ctx: Context, args) -> None:
    r = ctx.client().post(f"/access-requests/{args.id}/decision", json={"status": args.decision})
    print(f"Request {r['id']}: you set {args.decision}. Now: {describe_state(r)}")


def revoke(ctx: Context, args) -> None:
    r = ctx.client().post(f"/access-requests/{args.id}/revoke")
    print(f"Request {r['id']}: {describe_state(r)}")
