from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database import engine
from logic.errors import AppError
from routers.access_requests import router as access_requests_router
from routers.auth import router as auth_router
from routers.scheduler.jobs import router as jobs_router
from routers.scheduler.metrics import router as metrics_router
from routers.professors import router as professors_router


# Tables are created/changed ONLY by Alembic:  alembic upgrade head
app = FastAPI(title="Prayog API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],  # your frontend URL(s)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Every AppError raised in logic/ becomes a proper HTTP response here,
# so routes don't need try/except blocks.
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    content = {"detail": str(exc)}
    if getattr(exc, "code", None):          # machine-readable code, e.g. a rejected GPU request
        content["code"] = exc.code
    return JSONResponse(status_code=exc.status_code, content=content)


app.include_router(auth_router)
app.include_router(professors_router)
app.include_router(access_requests_router)
app.include_router(jobs_router)
app.include_router(metrics_router)


@app.get("/health")
def health_check():
    # Checks the API is up AND Postgres is reachable
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unreachable"},
        )
    return {"status": "ok", "database": "ok"}