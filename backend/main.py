from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database import engine
from logic.errors import AppError
from routers.access_requests import router as access_requests_router
from routers.admin_mlflow import router as admin_mlflow_router
from routers.auth import router as auth_router
from routers.handover import router as handover_router
from routers.mlflow import router as mlflow_router
from routers.mlflow_runs import router as mlflow_runs_router
from routers.mlflow_professor import router as mlflow_professor_router
from routers.scheduler.admin import router as scheduler_admin_router
from routers.scheduler.agent import router as agent_router
from routers.scheduler.agent_runs import router as agent_runs_router
from routers.scheduler.jobs import router as jobs_router
from routers.scheduler.metrics import router as metrics_router
from routers.professors import router as professors_router
from routers.rebuilds import router as rebuilds_router
from integration.steward_bridge import install_steward
from logic.mlflow_release import register_mlflow_hooks

app = FastAPI(title="Prayog API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
app.include_router(agent_router)
app.include_router(agent_runs_router)
app.include_router(scheduler_admin_router)
app.include_router(rebuilds_router)
app.include_router(admin_mlflow_router)
app.include_router(mlflow_router)
app.include_router(mlflow_runs_router)
app.include_router(mlflow_professor_router)
app.include_router(handover_router)

# A GPU job's end reaches its ML Flow run, inside the release transaction.
register_mlflow_hooks()

# The Steward (steward/): API and UI under /steward, and the run-seal hook on
# lifecycle.release(). If it is not configured the platform runs without it.
STEWARD_INSTALLED = install_steward(app)


@app.get("/health")
def health_check():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unreachable"},
        )
    return {"status": "ok", "database": "ok"}
