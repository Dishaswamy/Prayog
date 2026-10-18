"""
Access request flow

1. Student / researcher submits a request          -> all statuses PENDING
2. Professor and admin each decide APPROVED/REJECTED
   - Either one REJECTED  -> final REJECTED (but they can still change their mind)
   - Both APPROVED        -> final APPROVED, row copied into Student/Researcher,
                             GPU scheduling context created from the purpose,
                             request is LOCKED (no more decisions)
   Students are revoked automatically (reason EXPIRED) once their end date passes.
   If the user still has unfinished jobs, the revoke is pending: no new work is
   accepted, the jobs finish, and the revoke completes after the last one ends.
3. Only an admin can remove approved access          -> final REVOKED,
                                                        Student/Researcher row and
                                                        scheduling context deleted,
                                                        user may apply again
"""

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from logic.auth import normalize_email
from logic.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from logic.scheduler.domain import AccountType, JobState
from models import AccessRequest, Job, Lab, Professor, Researcher, SchedulingContext, Student, User

VALID_ROLES = {"STUDENT", "RESEARCHER"}
VALID_PURPOSES = {"COURSE_PROJECT", "RESEARCH_PROJECT"}
VALID_DECISIONS = {"APPROVED", "REJECTED"}
FINAL_STATUSES = {"PENDING", "APPROVED", "REJECTED", "REVOKED"}

# Requests in these states still "hold" the user's slot and roll number.
# REVOKED is the only state that frees them.
ACTIVE_STATUSES = ["PENDING", "REJECTED", "APPROVED"]

# A job in one of these states is not finished: its owner's access cannot end yet.
UNFINISHED_JOB_STATES = [JobState.IN_PROCESS, JobState.QUEUED, JobState.SCHEDULED, JobState.RUNNING]


# ─────────────────────────────────────────────
# 1. User submits an access request
# ─────────────────────────────────────────────
def create_access_request(
    db: Session,
    current_user: User,
    name: str,
    role: str,
    course: str,
    branch: str,
    professor_email: str,
    roll_number: str,
    start_date: datetime,
    end_date: datetime | None,
    purpose: str | None = None,
) -> AccessRequest:
    role = role.upper()
    roll_number = roll_number.strip()

    if role not in VALID_ROLES:
        raise BadRequestError("Role must be STUDENT or RESEARCHER")

    # Researchers always do research; students must say which kind of access they need
    if role == "RESEARCHER":
        if purpose not in (None, "RESEARCH_PROJECT"):
            raise BadRequestError("Researchers can only request RESEARCH_PROJECT access")
        purpose = "RESEARCH_PROJECT"
    elif purpose not in VALID_PURPOSES:
        raise BadRequestError("Purpose is required for students: COURSE_PROJECT or RESEARCH_PROJECT")

    # Requested role must match the registered role
    if current_user.role != role:
        raise ForbiddenError(
            f"Your registered role is {current_user.role}, but you selected {role}"
        )

    # One active request per user (a revoked user can apply again)
    existing = (
        db.query(AccessRequest)
        .filter(
            AccessRequest.user_id == current_user.id,
            AccessRequest.final_status.in_(ACTIVE_STATUSES),
        )
        .first()
    )
    if existing:
        raise ConflictError(
            f"You already have an access request (status: {existing.final_status})"
        )

    # Professor lookup by email (unique)
    professor = (
        db.query(Professor)
        .filter(Professor.email == normalize_email(professor_email))
        .first()
    )
    if not professor:
        raise NotFoundError("Professor with the given email was not found")

    # Dates
    if start_date is None:
        raise BadRequestError("Start date is required")

    if role == "STUDENT":
        if end_date is None:
            raise BadRequestError("End date is required for students")
        if end_date <= start_date:
            raise BadRequestError("End date must be after start date")
    else:
        end_date = None   # researchers have no end date

    # Roll number must not belong to an approved user or another active request
    roll_taken = (
        db.query(Student).filter(Student.roll_number == roll_number).first()
        or db.query(Researcher).filter(Researcher.roll_number == roll_number).first()
        or db.query(AccessRequest)
        .filter(
            AccessRequest.roll_number == roll_number,
            AccessRequest.final_status.in_(ACTIVE_STATUSES),
        )
        .first()
    )
    if roll_taken:
        raise ConflictError("This roll number is already in use")

    access_request = AccessRequest(
        user_id=current_user.id,
        name=name,
        role=role,
        course=course,
        branch=branch,
        purpose=purpose,
        professor_id=professor.id,
        roll_number=roll_number,
        start_date=start_date,
        end_date=end_date,
        status_professor="PENDING",
        status_admin="PENDING",
        final_status="PENDING",
    )

    db.add(access_request)
    db.commit()
    db.refresh(access_request)
    return access_request


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _get_request_for_update(db: Session, request_id: int) -> AccessRequest:
    """Fetch and row-lock (Postgres/MySQL) so professor and admin can't race."""
    access_request = (
        db.query(AccessRequest)
        .filter(AccessRequest.id == request_id)
        .with_for_update()
        .first()
    )
    if not access_request:
        raise NotFoundError("Access request not found")
    return access_request


def _get_professor_profile(db: Session, user: User) -> Professor:
    professor = db.query(Professor).filter(Professor.user_id == user.id).first()
    if not professor:
        raise NotFoundError("Professor profile not found for this account")
    return professor


def _compute_final_status(access_request: AccessRequest) -> str:
    """Final status is always derived from the two decisions."""
    p = access_request.status_professor
    a = access_request.status_admin

    if p == "REJECTED" or a == "REJECTED":
        return "REJECTED"
    if p == "APPROVED" and a == "APPROVED":
        return "APPROVED"
    return "PENDING"


def _create_role_row(db: Session, access_request: AccessRequest) -> None:
    """Copy the approved request into Student / Researcher. No commit here."""
    user = db.query(User).filter(User.id == access_request.user_id).first()
    if not user:
        raise NotFoundError("User associated with access request not found")

    common = dict(
        user_id=access_request.user_id,
        name=access_request.name,
        email=user.email,
        roll_number=access_request.roll_number,
        course=access_request.course,
        branch=access_request.branch,
        professor_id=access_request.professor_id,
        start_date=access_request.start_date,
    )

    if access_request.role == "STUDENT":
        db.add(Student(**common, end_date=access_request.end_date))
    elif access_request.role == "RESEARCHER":
        db.add(Researcher(**common))
    else:
        raise BadRequestError(f"Invalid access request role: {access_request.role}")


def _apply_scheduling_context(db: Session, access_request: AccessRequest) -> None:
    """
    Turn an approved request into the GPU context the scheduler reads. No commit here.

    COURSE_PROJECT   -> course account in the institute's central lab (teaching pool)
    RESEARCH_PROJECT -> research account in the supervising professor's lab
    """
    if access_request.purpose == "COURSE_PROJECT":
        account_type = AccountType.COURSE
        lab = db.query(Lab).filter(Lab.is_central_owner.is_(True)).order_by(Lab.id).first()
        if not lab:
            raise ConflictError("No central lab is configured for course projects")
    else:
        account_type = AccountType.RESEARCH
        professor = db.get(Professor, access_request.professor_id)
        lab = db.get(Lab, professor.lab_id) if professor and professor.lab_id else None
        if not lab:
            raise ConflictError(
                "The professor has no lab assigned. An admin must assign one before "
                "research access can be approved"
            )

    context = db.get(SchedulingContext, access_request.user_id)
    if context is None:
        db.add(SchedulingContext(
            user_id=access_request.user_id, lab_id=lab.id, account_type=account_type
        ))
    else:
        context.lab_id = lab.id
        context.account_type = account_type
        if account_type != AccountType.COURSE:
            context.course_cap_gpu_hours = None   # a cap only applies to course accounts


# ─────────────────────────────────────────────
# 2. Professor / Admin approve or reject
# ─────────────────────────────────────────────
def decide_access_request(
    db: Session,
    request_id: int,
    current_user: User,
    decision: str,
) -> AccessRequest:
    decision = decision.upper()
    if decision not in VALID_DECISIONS:
        raise BadRequestError("Decision must be APPROVED or REJECTED")

    if current_user.role not in {"PROFESSOR", "ADMIN"}:
        raise ForbiddenError("Only professors and admins can decide on access requests")

    access_request = _get_request_for_update(db, request_id)

    # Approved by both -> locked. Removal is a separate admin action (revoke).
    if access_request.final_status == "APPROVED":
        raise ConflictError(
            "This request is already approved and locked. Only an admin can revoke access."
        )
    if access_request.final_status == "REVOKED":
        raise ConflictError("This access has been revoked. The user must submit a new request.")

    # Record this person's decision (they may change it while not locked)
    if current_user.role == "PROFESSOR":
        professor = _get_professor_profile(db, current_user)
        if access_request.professor_id != professor.id:
            raise ForbiddenError("This request is not addressed to you")
        access_request.status_professor = decision
    else:  # ADMIN
        access_request.status_admin = decision

    new_final = _compute_final_status(access_request)

    if new_final == "APPROVED":
        _create_role_row(db, access_request)
        _apply_scheduling_context(db, access_request)

    access_request.final_status = new_final

    try:
        db.commit()
    except IntegrityError:
        # e.g. roll number / email already present in Student/Researcher
        db.rollback()
        raise ConflictError(
            "Could not finalize: this user, email or roll number already exists "
            "in the approved records"
        )

    db.refresh(access_request)
    return access_request


# ─────────────────────────────────────────────
# 3. Admin removes an already-approved user (separate action)
# ─────────────────────────────────────────────
def revoke_access(db: Session, request_id: int, current_user: User) -> AccessRequest:
    if current_user.role != "ADMIN":
        raise ForbiddenError("Only an admin can revoke access")

    access_request = _get_request_for_update(db, request_id)

    if access_request.final_status != "APPROVED":
        raise ConflictError("Only approved access can be revoked")

    _end_access(db, access_request, "ADMIN", datetime.utcnow())

    db.commit()
    db.refresh(access_request)
    return access_request


def _has_unfinished_jobs(db: Session, user_id: int) -> bool:
    return db.query(Job.id).filter(Job.user_id == user_id, Job.state.in_(UNFINISHED_JOB_STATES)).first() is not None


def _end_access(db: Session, access_request: AccessRequest, reason: str, now: datetime) -> bool:
    """
    End a user's access. If they still have unfinished jobs, only mark the revoke as
    pending: the jobs run to completion and the revoke completes when the last one
    ends (see sweep_access_revocations). Otherwise revoke now. Returns True if revoked.
    No commit here.
    """
    reason = access_request.revoke_reason or reason     # the first reason sticks
    if _has_unfinished_jobs(db, access_request.user_id):
        access_request.revoke_reason = reason
        if access_request.revoke_requested_at is None:
            access_request.revoke_requested_at = now
        return False
    _revoke(db, access_request, reason, now)
    return True


def _revoke(db: Session, access_request: AccessRequest, reason: str, now: datetime) -> None:
    """
    Take access away: remove the approved role row and the GPU context, and mark the
    request REVOKED. The request stays as the record of the access and why it ended.
    No commit here.
    """
    model = Student if access_request.role == "STUDENT" else Researcher
    db.query(model).filter(model.user_id == access_request.user_id).delete()
    # No role row, no GPU access: the scheduler reads only this table
    db.query(SchedulingContext).filter(
        SchedulingContext.user_id == access_request.user_id
    ).delete()

    access_request.final_status = "REVOKED"
    access_request.revoked_at = now
    access_request.revoke_reason = reason


def sweep_access_revocations(db: Session, now: datetime | None = None) -> int:
    """
    Run periodically. Ends access that is due:
      - approved students whose end date has passed (reason EXPIRED); researchers
        have no end date and professors are not requests, so only an admin removes them;
      - revokes that were pending because of unfinished jobs, once those have finished.
    Safe to run repeatedly and from several processes (locked rows are skipped).
    Returns how many were revoked.
    """
    now = now or datetime.utcnow()
    due = (
        db.query(AccessRequest)
        .filter(
            AccessRequest.final_status == "APPROVED",
            or_(
                AccessRequest.revoke_requested_at.is_not(None),
                and_(
                    AccessRequest.role == "STUDENT",
                    AccessRequest.end_date.is_not(None),
                    AccessRequest.end_date <= now,
                ),
            ),
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    revoked = sum(_end_access(db, access_request, "EXPIRED", now) for access_request in due)
    db.commit()
    return revoked


# ─────────────────────────────────────────────
# 4. Reading requests (for the portal screens)
# ─────────────────────────────────────────────
def list_my_requests(db: Session, current_user: User) -> list[AccessRequest]:
    return (
        db.query(AccessRequest)
        .filter(AccessRequest.user_id == current_user.id)
        .order_by(AccessRequest.id.desc())
        .all()
    )


def list_requests_for_reviewer(
    db: Session,
    current_user: User,
    final_status: str | None = None,
) -> list[AccessRequest]:
    query = db.query(AccessRequest)

    if current_user.role == "PROFESSOR":
        professor = _get_professor_profile(db, current_user)
        query = query.filter(AccessRequest.professor_id == professor.id)
    elif current_user.role != "ADMIN":
        raise ForbiddenError("Only professors and admins can view requests")

    if final_status:
        final_status = final_status.upper()
        if final_status not in FINAL_STATUSES:
            raise BadRequestError(f"status must be one of {sorted(FINAL_STATUSES)}")
        query = query.filter(AccessRequest.final_status == final_status)

    return query.order_by(AccessRequest.id.desc()).all()


def get_access_request(db: Session, request_id: int, current_user: User) -> AccessRequest:
    """Applicant, their professor, or any admin can view one request."""
    access_request = db.query(AccessRequest).filter(AccessRequest.id == request_id).first()
    if not access_request:
        raise NotFoundError("Access request not found")

    if current_user.role == "ADMIN" or access_request.user_id == current_user.id:
        return access_request

    if current_user.role == "PROFESSOR":
        professor = _get_professor_profile(db, current_user)
        if access_request.professor_id == professor.id:
            return access_request

    raise ForbiddenError("You are not allowed to view this request")
