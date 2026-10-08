"""
Create the first ADMIN account (admins can't self-register).

Run from the backend folder:
    python -m scripts.create_admin admin@iitk.ac.in "StrongPassword123"
"""

import sys

from database import SessionLocal
from logic.auth import create_user_record
from logic.errors import AppError


def create_admin(email: str, password: str):
    db = SessionLocal()
    try:
        user = create_user_record(db, email, password, "ADMIN")
        db.commit()
        print(f"Admin created: {user.email} (id={user.id})")
    except AppError as e:
        db.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print('Usage: python -m scripts.create_admin <email> "<password>"')
        sys.exit(1)
    create_admin(sys.argv[1], sys.argv[2])