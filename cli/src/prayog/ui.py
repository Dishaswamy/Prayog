import sys
from typing import Optional, Sequence

from prayog.errors import CliError, USAGE


def prompt(label: str, value: Optional[str], flag: str) -> str:
    """The flag's value, or ask for it when a person is at the keyboard."""
    if value:
        return value
    if not sys.stdin.isatty():
        raise CliError(f"Missing {flag}.", USAGE)
    answer = input(f"{label}: ").strip()
    if not answer:
        raise CliError(f"{label} is required.", USAGE)
    return answer


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)])


def day(value: Optional[str]) -> str:
    return value[:10] if value else "-"
