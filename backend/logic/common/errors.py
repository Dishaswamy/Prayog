"""
App-level errors. Logic functions raise these; main.py turns them into
HTTP responses with the right status code, so routes don't need try/except.
"""


class AppError(Exception):
    status_code = 400


class BadRequestError(AppError):
    """Input is invalid (wrong dates, missing field, ...)."""
    status_code = 400


class ForbiddenError(AppError):
    """Logged in, but not allowed to do this."""
    status_code = 403


class NotFoundError(AppError):
    """The thing being asked for doesn't exist."""
    status_code = 404


class ConflictError(AppError):
    """Clashes with existing data (duplicate email, request already locked, ...)."""
    status_code = 409
