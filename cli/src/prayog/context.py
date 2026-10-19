from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from prayog.client import Client
from prayog.credentials import Credentials
from prayog.errors import CliError, NOT_AUTHENTICATED


@dataclass
class Context:
    creds: Optional[Credentials]
    transport: Optional[httpx.BaseTransport] = None    # tests inject a mock transport
    # Supplied by cli.main so `help` can describe exactly what this person can use.
    build_parser: Optional[Callable] = None
    available: Optional[Callable] = None
    denial: Optional[Callable] = None

    def client(self) -> Client:
        """A client for the saved login. Commands marked protected never see creds=None."""
        if self.creds is None:
            raise CliError("You are not logged in. Run `prayog login`.", NOT_AUTHENTICATED)
        return Client(self.creds.api_url, self.creds.token, transport=self.transport)
