"""
The web app: backend/web, served by the API at /app with no build step.

The Steward's page modules ship with the prayog_steward package. They are served at
/app/steward whether or not the Steward is running, because the app shares their
client and stylesheet; it offers the Steward's screens only when /steward answers.
"""

from pathlib import Path

import prayog_steward
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
STEWARD_UI_DIR = Path(prayog_steward.__file__).parent / "api" / "ui"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def install_web(app: FastAPI) -> None:
    @app.middleware("http")
    async def _web_headers(request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        if request.url.path == "/app" or request.url.path.startswith("/app/"):
            h = response.headers
            h.setdefault("Content-Security-Policy", CSP)
            h.setdefault("X-Content-Type-Options", "nosniff")
            h.setdefault("X-Frame-Options", "DENY")
            h.setdefault("Referrer-Policy", "no-referrer")
            h.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/app/")

    app.mount("/app/steward", StaticFiles(directory=STEWARD_UI_DIR), name="web-steward")
    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")
