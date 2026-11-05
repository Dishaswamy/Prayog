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