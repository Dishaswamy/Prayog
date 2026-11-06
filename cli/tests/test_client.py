import httpx
import pytest

from prayog.client import Client
from prayog.errors import CliError


def test_malformed_validation_error_is_reported_as_cli_error():
    def handler(request):
        return httpx.Response(422, json={"detail": [None]})

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError):
        client.get("/test")

@pytest.mark.parametrize(
    "detail",
    [
        [None],
        ["unexpected string"],
        [{"loc": "body.name", "msg": "invalid location"}],
        [{"loc": None, "msg": None}],
    ],
)
def test_malformed_validation_error_items_do_not_crash(detail):
    def handler(request):
        return httpx.Response(422, json={"detail": detail})

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError):
        client.get("/test")


def test_non_json_success_response_is_reported_as_cli_error():
    def handler(request):
        return httpx.Response(200, text="not valid JSON")

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError):
        client.get("/test")


def test_non_object_error_response_is_reported_as_cli_error():
    def handler(request):
        return httpx.Response(400, json=["unexpected", "response"])

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError):
        client.get("/test")


def test_server_error_is_reported_as_cli_error():
    def handler(request):
        return httpx.Response(500, json={"detail": "Internal error"})

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError):
        client.get("/test")


# --- Additional client.py failure-path coverage ---


@pytest.mark.parametrize("body", ["Server temporarily unavailable", ""])
def test_non_json_error_response_becomes_generic_server_error(body):
    def handler(request):
        return httpx.Response(502, text=body)

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError) as exc:
        client.get("/test")

    assert "HTTP 502" in exc.value.message


def test_upload_network_error_becomes_server_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError) as exc:
        client.upload("/artifacts", {}, b"payload")

    assert exc.value.code == "SERVER_UNREACHABLE"
    assert "Could not reach the Prayog server" in exc.value.message


def test_stream_network_error_becomes_server_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = Client(
        "http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CliError) as exc:
        with client.stream("/artifacts"):
            pass

    assert exc.value.code == "SERVER_UNREACHABLE"
    assert "Could not reach the Prayog server" in exc.value.message
