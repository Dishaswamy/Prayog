from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Registered to app Users
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


# Professor
class Professor(Base):
    __tablename__ = "professors"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    department: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


# Access Request status
class AccessRequest(Base):
    __tablename__ = "access_requests"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # Applicant's registered Prayog account
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    course: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    branch: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Mapped from professor name + email submitted in the request
    professor_id: Mapped[int] = mapped_column(
        ForeignKey("professors.id"),
        nullable=False,
    )

    # Required for students/researchers
    roll_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=False,
    )

    start_date: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )

    # NULL for researchers
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    status_professor: Mapped[str] = mapped_column(
        String(20),
        default="PENDING",
        nullable=False,
    )

    status_admin: Mapped[str] = mapped_column(
        String(20),
        default="PENDING",
        nullable=False,
    )

    final_status: Mapped[str] = mapped_column(
        String(20),
        default="PENDING",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


# MLFlow project metadata
class MLFlowProject(Base):
    __tablename__ = "mlflow_projects"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # Owner of the MLFlow project
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    project_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Location of the actual project files on Prayog storage
    project_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


# MLFlow experiment/run metadata
class MLFlowRun(Base):
    __tablename__ = "mlflow_runs"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("mlflow_projects.id"),
        nullable=False,
    )

    # User who created the run
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="CREATED",
        nullable=False,
    )

    # Flexible experiment metadata stored as PostgreSQL JSONB
    environment: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    hyperparameters: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    metrics: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    gpu_details: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    results: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
