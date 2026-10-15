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
from routers.mlflow import router as mlflow_router
from routers.professors import router as professors_router

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
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


app.include_router(auth_router)
app.include_router(professors_router)
app.include_router(access_requests_router)
app.include_router(admin_mlflow_router)
app.include_router(mlflow_router)


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
