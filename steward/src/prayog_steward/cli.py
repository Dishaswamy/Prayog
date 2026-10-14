"""``prayog steward ...``: a Typer sub-app the platform CLI mounts (``app.add_typer``).

Talks to the Steward API over HTTPS with the CLI's credential. The platform CLI passes
its own credential store through ``PRAYOG_TOKEN``/``--token``. ``report_rebuild_failure``
is what the CLI calls when a ``--rebuild`` attempt ends REBUILD_FAILED (FR-STW-003, 004).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import httpx
import typer

log = logging.getLogger("prayog_steward.cli")
app = typer.Typer(
    help="Steward: diagnoses, proposals, rebuild help and optimisation.", no_args_is_help=True
)

ApiUrl = Annotated[str, typer.Option(envvar="PRAYOG_API_URL", help="Prayog API base URL")]
Token = Annotated[str, typer.Option(envvar="PRAYOG_TOKEN", help="Your Prayog credential")]


class StewardClient:
    def __init__(self, api_url: str, token: str, client: httpx.Client | None = None) -> None:
        self.base = api_url.rstrip("/") + "/steward"
        self.http = client or httpx.Client(timeout=180.0)
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, **kw: Any) -> Any:
        response = self.http.request(method, self.base + path, headers=self.headers, **kw)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:200]}
        if response.status_code >= 400:
            raise StewardError(response.status_code, body.get("detail", "request failed"))
        return body


class StewardError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


def report_rebuild_failure(
    client: StewardClient, attempt_id: str, snapshot_id: str, reason: str
) -> str:
    """The message the CLI prints when a rebuild attempt fails (FR-STW-003). If no secure
    link can be issued, say so plainly. There is no insecure fallback (FR-STW-004, A16).
    The attempt stays REBUILD_FAILED either way."""
    header = (
        f"Rebuild failed. Attempt ID: {attempt_id}. Snapshot ID: {snapshot_id}.\nReason: {reason}"
    )
    try:
        link = client.call("POST", f"/attempts/{attempt_id}/link")
    except (StewardError, httpx.HTTPError) as exc:
        log.warning(
            "steward link generation failed for attempt %s: %s", attempt_id, type(exc).__name__
        )
        return (
            header + "\nSteward: unavailable right now. Retry later with: "
            f"prayog steward link {attempt_id}"
        )
    return (
        header + "\nSteward: available. To analyze this error with Steward, click here: "
        f"{link['url']}\n(single-use link, expires in 15 minutes)"
    )


def _client(api_url: str, token: str) -> StewardClient:
    return StewardClient(api_url, token)


def _fail(exc: StewardError) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@app.command()
def link(attempt_id: str, api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """Get a fresh single-use Steward link for a failed rebuild attempt."""
    try:
        issued = _client(api_url, token).call("POST", f"/attempts/{attempt_id}/link")
    except StewardError as exc:
        _fail(exc)
    typer.echo(issued["message"])


@app.command()
def optimize(attempt_id: str, api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """Advisory suggestions for a REBUILD_SUCCESS attempt (FR-STW-006)."""
    try:
        answer = _client(api_url, token).call("POST", f"/attempts/{attempt_id}/optimize")
    except StewardError as exc:
        _fail(exc)
    for notice in answer.get("notices", []):
        typer.echo(f"note: {notice}")
    for i, s in enumerate(answer.get("suggestions", []), 1):
        typer.echo(f"{i}. [{s['kind']}] {s['text']}  (evidence: {', '.join(s['cites'])})")
    for rid, src in answer.get("sources", {}).items():
        if src.get("title"):
            typer.echo(f"   {rid}: {src['title']}  {src.get('url') or ''}")


@app.command()
def diagnosis(run_id: str, api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """Show the Steward's diagnoses of a failed run."""
    try:
        items = _client(api_url, token).call("GET", f"/runs/{run_id}/diagnoses")
    except StewardError as exc:
        _fail(exc)
    if not items:
        typer.echo("No diagnosis yet. The Steward works after the run is sealed.")
    for d in items:
        typer.echo(f"[{d['created_at']}] {d['outcome']}  class={d['classification']}")
        for c in d["claims"]:
            cites = ", ".join(x["record_id"] for x in c["cites"])
            typer.echo(f"  - {c['text']}  [{cites}]")
        if d["outcome"] == "unavailable":
            typer.echo("  Steward was unavailable for this run; scheduling was unaffected.")


@app.command()
def proposals(api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """List your pending corrected-job proposals."""
    try:
        items = _client(api_url, token).call("GET", "/proposals", params={"state": "PROPOSED"})
    except StewardError as exc:
        _fail(exc)
    if not items:
        typer.echo("No pending proposals.")
    for p in items:
        typer.echo(f"{p['proposal_id']}  run {p['run_id']}  {p['requested_gpu_hours']} GPU-h")
        for ch in p["changes"]:
            typer.echo(f"    {ch['field']} -> {ch['new_value']}: {ch['reason']['text']}")


def _decide(verb: str, proposal_id: str, api_url: str, token: str) -> None:
    try:
        client = _client(api_url, token)
        if verb == "approve":
            p = client.call("GET", f"/proposals/{proposal_id}")
            typer.echo(json.dumps({"from": p["current_spec"], "to": p["proposed_spec"]}, indent=2))
            typer.echo(f"Requested GPU-hours: {p['requested_gpu_hours']}")
            typer.confirm("Submit this corrected job?", abort=True)
        result = client.call("POST", f"/proposals/{proposal_id}/{verb}")
    except StewardError as exc:
        _fail(exc)
    if result.get("refusal"):
        typer.echo(f"Not queued: {result['refusal']}")
    elif result.get("submitted_job_id"):
        typer.echo(f"Queued as job {result['submitted_job_id']}.")
    else:
        typer.echo(f"Proposal {result['state'].lower()}.")


@app.command()
def approve(proposal_id: str, api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """Review the exact change and its GPU-hours, then approve (FR-GPU-187)."""
    _decide("approve", proposal_id, api_url, token)


@app.command()
def reject(proposal_id: str, api_url: ApiUrl = "http://localhost:8000", token: Token = "") -> None:
    """Reject a proposal. It is kept with its outcome."""
    _decide("reject", proposal_id, api_url, token)


if __name__ == "__main__":
    app()
