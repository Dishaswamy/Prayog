import pytest

from prayog.context import Context
from prayog.errors import CliError, NOT_AUTHENTICATED


def test_client_rejects_missing_credentials():
    ctx = Context(creds=None)

    with pytest.raises(CliError) as exc:
        ctx.client()

    assert exc.value.code == NOT_AUTHENTICATED
    assert "You are not logged in" in str(exc.value)