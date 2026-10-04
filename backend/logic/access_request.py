from sqlalchemy.orm import Session
from models import User, Professor, AccessRequest


def create_access_request(
    db: Session,
    current_user: User,
    name: str,
    role: str,
    course: str,
    branch: str,
    professor_name: str,
    professor_email: str,
    roll_number: str,
    start_date,
    end_date,
):
    # 1. Check that the requested role matches
    #    the role with which the user registered.
    if current_user.role != role:
        raise ValueError(
            f"Your registered role is {current_user.role}, "
            f"but you selected {role}"
        )

    # 2. Find the professor using the name + email
    professor = (
        db.query(Professor)
        .filter(
            Professor.name == professor_name,
            Professor.email == professor_email,
        )
        .first()
    )

    if not professor:
        raise ValueError(
            "Professor with the given name and email was not found"
        )

    # 3. Check dates for students
    if role == "STUDENT":
        if start_date is None or end_date is None:
            raise ValueError(
                "Start date and end date are required for students"
            )

        if end_date <= start_date:
            raise ValueError(
                "End date must be after start date"
            )

    # 4. Researchers do not have an end date
    if role == "RESEARCHER":
        end_date = None

    # 5. Create access request
    access_request = AccessRequest(
        user_id=current_user.id,
        name=name,
        role=role,
        course=course,
        branch=branch,
        professor_id=professor.id,
        roll_number=roll_number,
        start_date=start_date,
        end_date=end_date,
        status_professor="PENDING",
        status_admin="PENDING",
        final_status="PENDING",
    )

    # 6. Save
    db.add(access_request)
    db.commit()
    db.refresh(access_request)

    return access_request