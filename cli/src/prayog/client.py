"""HTTP client for the Prayog API. Turns every failure into a CliError with a next step.

The server decides authorization; this only translates its answers (spec NFR-AUTH-03, -09, -10).
Anything that cannot be established reliably fails closed (NFR-AUTH-08).
"""
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import httpx

from prayog import errors
from prayog.errors import CliError

LOGIN_HINT = "Run `prayog login`."
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TRANSFER_TIMEOUT = 300.0     # file bytes in either direction; ordinary calls keep the short default


def is_secure_enough(api_url: str) -> bool:
    """https anywhere, or plain http to this machine only (NFR-AUTH-02)."""
    parsed = urlparse(api_url)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS)


def _detail(response: httpx.Response) -> tuple:
    """(message, machine code) from an error body."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}", None
    if not isinstance(body, dict):
        return f"HTTP {response.status_code}", None
    detail, code = body.get("detail"), body.get("code")
    if isinstance(detail, list):    # 422: one line per problem
        lines = []
        for item in detail:
            where = ".".join(str(p) for p in item.get("loc", []) if p != "body")
            lines.append(f"{where}: {item.get('msg')}" if where else str(item.get("msg")))
        detail = "; ".join(lines)
    return (str(detail) if detail else f"HTTP {response.status_code}"), code


def error_for(response: httpx.Response, *, authenticated: bool) -> CliError:
    message, code = _detail(response)
    status = response.status_code
    if status == 401:
        if not authenticated:
            return CliError(message, errors.INVALID_CREDENTIAL)
        if "expired" in message.lower():
            return CliError(f"Your login session has expired. {LOGIN_HINT}", errors.SESSION_EXPIRED)
        return CliError(f"The server did not accept your saved login ({message}). {LOGIN_HINT}",
                        errors.INVALID_CREDENTIAL)
    if status == 403:
        if code == "ACCESS_EXPIRED":
            return CliError("Your approved access period has ended. Submit a new access request in the web portal "
                            "or ask your professor/admin about an extension.", errors.ACCESS_EXPIRED)
        if code == "ACCESS_REVOKED":
            return CliError("Your access has been revoked. Contact an admin, or submit a new access request in the web portal.",
                            errors.ACCESS_REVOKED)
        if code == "AUTHORIZATION_INACTIVE":
            return CliError(f"{message} Check `prayog access status`; access must be approved first.",
                            errors.INSUFFICIENT_PERMISSION)
        return CliError(message, errors.INSUFFICIENT_PERMISSION)
    if status >= 500:
        return CliError(f"The Prayog server had an internal error (HTTP {status}). Try again later.")
    return CliError(message, errors.REQUEST_FAILED)


class Client:
    def __init__(self, api_url: str, token: Optional[str] = None, *,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 15.0):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self._http = httpx.Client(base_url=self.api_url, transport=transport, timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _unreachable(self, exc: httpx.HTTPError) -> CliError:
        return CliError(f"Could not reach the Prayog server at {self.api_url} ({type(exc).__name__}). "
                        "Check your connection and `--api-url`; nothing was changed.",
                        errors.SERVER_UNREACHABLE)

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise error_for(response, authenticated=self.token is not None)
        try:
            return response.json()
        except ValueError as exc:
            raise CliError("The server sent an unreadable reply.") from exc

    def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None) -> Any:
        try:
            response = self._http.request(method, path, json=json, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        return self._json(response)

    def upload(self, path: str, params: dict, payload: Any, method: str = "POST") -> Any:
        """Send raw bytes (bytes, or an iterator of bytes for a large file); returns the server's JSON."""
        try:
            response = self._http.request(method, path, params=params, content=payload, timeout=TRANSFER_TIMEOUT,
                                          headers={**self._headers(), "Content-Type": "application/octet-stream"})
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        return self._json(response)

    @contextmanager
    def stream(self, path: str) -> Iterator[httpx.Response]:
        """GET a large body without holding it in memory; read it with response.iter_bytes()."""
        try:
            with self._http.stream("GET", path, headers=self._headers(), timeout=TRANSFER_TIMEOUT) as response:
                if response.status_code >= 400:
                    response.read()
                    raise error_for(response, authenticated=self.token is not None)
                yield response
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)
