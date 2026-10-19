"""
Is this authenticated user currently authorized to use Prayog's protected workflow?
(Spec sections 4.2 and 15: authentication says who you are, this says whether you may.)

  STUDENT / RESEARCHER  an approved access record exists (a row in students/researchers, which
                        approval creates and revoking deletes) and, for a student, the end date has
                        not passed by the SERVER's clock. The end date is checked on every request,
                        so an expiry is enforced even before the periodic sweep revokes the record.
  PROFESSOR             has a professor profile (institutional access).
  ADMIN                 passes; the routes still apply their own role guards.

When there is no access, the reason is one of the spec's codes, so a client can tell them apart:
  ACCESS_EXPIRED            the approved period has ended
  ACCESS_REVOKED            an admin took access away
  INSUFFICIENT_PERMISSION   never approved: no request, or still pending, or rejected
Anything that cannot be established fails closed (NFR-AUTH-08).

A revoke that is only pending (the user still has unfinished jobs) leaves access in place here: the
scheduler already accepts no new work from them, and they need to read the results of what is running.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from logic.errors import AppError
from models import AccessRequest, Professor, Researcher, Student, User

ACCESS_EXPIRED = "ACCESS_EXPIRED"
ACCESS_REVOKED = "ACCESS_REVOKED"
INSUFFICIENT_PERMISSION = "INSUFFICIENT_PERMISSION"

PORTAL = "Request access in the Prayog web portal."


class AccessDenied(AppError):
    status_code = 403

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_active_access(db: Session, user: User, now: datetime | None = None) -> None:
    """Return if the user may use the protected workflow now; raise AccessDenied otherwise."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)      # naive UTC, like the database columns

    if user.role == "ADMIN":
        return
    if user.role == "PROFESSOR":
        if db.query(Professor.id).filter(Professor.user_id == user.id).first() is not None:
            return
        raise AccessDenied(INSUFFICIENT_PERMISSION, "No professor profile exists for this account. Contact an admin.")
    if user.role not in ("STUDENT", "RESEARCHER"):
        raise AccessDenied(INSUFFICIENT_PERMISSION, "Your role cannot use this feature.")

    model = Student if user.role == "STUDENT" else Researcher
    row = db.query(model).filter(model.user_id == user.id).first()
    if row is not None:
        end = getattr(row, "end_date", None)        # researchers have none
        if end is not None and end <= now:
            raise AccessDenied(ACCESS_EXPIRED, "Your approved access period has ended. " + PORTAL)
        return

    latest = (
        db.query(AccessRequest)
        .filter(AccessRequest.user_id == user.id)
        .order_by(AccessRequest.created_at.desc(), AccessRequest.id.desc())
        .first()
    )
    if latest is not None and latest.final_status == "REVOKED":
        if latest.revoke_reason == "EXPIRED":
            raise AccessDenied(ACCESS_EXPIRED, "Your approved access period has ended. " + PORTAL)
        raise AccessDenied(ACCESS_REVOKED, "Your access has been revoked. Contact an admin, or request access again in the web portal.")
    if latest is not None and latest.final_status == "PENDING":
        raise AccessDenied(INSUFFICIENT_PERMISSION, "Your access request is still waiting for approval.")
    if latest is not None and latest.final_status == "REJECTED":
        raise AccessDenied(INSUFFICIENT_PERMISSION, "Your access request was rejected. " + PORTAL)
    raise AccessDenied(INSUFFICIENT_PERMISSION, "You do not have approved access yet. " + PORTAL)
