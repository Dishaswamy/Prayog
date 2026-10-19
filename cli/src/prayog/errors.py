"""One error type for everything the CLI reports to the user.

`code` uses the vocabulary of the authorization decision flow (spec section 15), so
scripts and tests can tell the cases apart without parsing the message.
"""

NOT_AUTHENTICATED = "NOT_AUTHENTICATED"          # no local credential
INVALID_CREDENTIAL = "INVALID_CREDENTIAL"        # wrong login, or a token the server rejects
SESSION_EXPIRED = "SESSION_EXPIRED"              # the login token timed out (not the access period)
ACCESS_EXPIRED = "ACCESS_EXPIRED"                # authenticated, but the approved period has ended
ACCESS_REVOKED = "ACCESS_REVOKED"
INSUFFICIENT_PERMISSION = "INSUFFICIENT_PERMISSION"
SERVER_UNREACHABLE = "SERVER_UNREACHABLE"
REQUEST_FAILED = "REQUEST_FAILED"                # 400/404/409/422/5xx: the message carries the server's reason
USAGE = "USAGE"


class CliError(Exception):
    def __init__(self, message: str, code: str = REQUEST_FAILED):
        super().__init__(message)
        self.message = message
        self.code = code
