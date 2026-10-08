from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text

# Current time in UTC (Postgres now() otherwise follows the server's timezone)
UTC_NOW = text("(now() AT TIME ZONE 'utc')")
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Allowed values (kept in one place so the DB and the code agree)
ROLES = "('STUDENT', 'RESEARCHER', 'PROFESSOR', 'ADMIN')"
DECISIONS = "('PENDING', 'APPROVED', 'REJECTED')"
FINAL_STATUSES = "('PENDING', 'APPROVED', 'REJECTED', 'REVOKED')"


# ─────────────────────────────────────────────
# Registered app users (every login is a row here)
# ─────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN {ROLES}", name="ck_users_role"),
    )

    # Internal integer key: used in foreign keys and in the JWT "sub"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Public ID = part of the IITK email before "@"
    user_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Professor profile (one per PROFESSOR user, created by an admin)
# ─────────────────────────────────────────────
class Professor(Base):
    __tablename__ = "professors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Access request + both approvals
# ─────────────────────────────────────────────
class AccessRequest(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint("role IN ('STUDENT', 'RESEARCHER')", name="ck_ar_role"),
        CheckConstraint(f"status_professor IN {DECISIONS}", name="ck_ar_status_professor"),
        CheckConstraint(f"status_admin IN {DECISIONS}", name="ck_ar_status_admin"),
        CheckConstraint(f"final_status IN {FINAL_STATUSES}", name="ck_ar_final_status"),
        # Students: end date required and after start. Researchers: no end date.
        CheckConstraint(
            "(role = 'RESEARCHER' AND end_date IS NULL) OR "
            "(role = 'STUDENT' AND end_date IS NOT NULL AND end_date > start_date)",
            name="ck_ar_dates",
        ),
        Index("ix_ar_user_status", "user_id", "final_status"),
        Index("ix_ar_professor_status", "professor_id", "final_status"),
        Index("ix_ar_roll_number", "roll_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Applicant's registered account
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    # Resolved from the professor email submitted in the request
    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    # Required for both students and researchers
    roll_number: Mapped[str] = mapped_column(String(50), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # NULL for researchers

    status_professor: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    status_admin: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    final_status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, onupdate=func.timezone("utc", func.now()), nullable=False
    )


# ─────────────────────────────────────────────
# Approved students (row exists only while access is APPROVED)
# ─────────────────────────────────────────────
class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    roll_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )


# ─────────────────────────────────────────────
# Approved researchers (row exists only while access is APPROVED)
# ─────────────────────────────────────────────
class Researcher(Base):
    __tablename__ = "researchers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    roll_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    course: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(100), nullable=False)

    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), nullable=False)

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTC_NOW, nullable=False
    )