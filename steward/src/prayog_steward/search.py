"""Web search for S3 (ADR-S06).

The query is *built* from ``ErrorFacts`` (an exception class plus pinned package names and
versions) and checked against an allow-list shape. It is never free text from the user,
the log or the model, so no code, secret or PII can reach the search provider
(FR-STW-012, NFR-SEC-006). Results are untrusted, sanitised, and labelled external
(FR-STW-016, 017). Failures degrade to "unavailable" and never raise (A20).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx

from prayog_steward.rag.sanitiser import ErrorFacts

_SAFE_QUERY = re.compile(r"^[A-Za-z0-9_.\- ]{3,200}$")


class SearchState(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchOutcome:
    state: SearchState
    query: str | None
    results: tuple[SearchResult, ...] = ()


def build_query(facts: ErrorFacts, max_packages: int = 3) -> str | None:
    """``torch.cuda.OutOfMemoryError torch 2.3.1 error``, or None if there is nothing safe."""
    if facts.exception_class is None:
        return None
    parts = [facts.exception_class]
    for name, version in facts.packages[:max_packages]:
        parts += [name, version]
    query = " ".join([*parts, "error"])
    return query if _SAFE_QUERY.match(query) else None


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class TavilySearch:
    name = "tavily"

    def __init__(self, api_key: str, timeout: float) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._key = api_key

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        response = self._client.post(
            "https://api.tavily.com/search",
            json={"api_key": self._key, "query": query, "max_results": max_results},
        )
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json().get("results", [])
        return [
            SearchResult(i.get("title", ""), i.get("url", ""), i.get("content", ""))
            for i in items[:max_results]
        ]


class BraveSearch:
    name = "brave"

    def __init__(self, api_key: str, timeout: float) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._key = api_key

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        response = self._client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
        )
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json().get("web", {}).get("results", [])
        return [
            SearchResult(i.get("title", ""), i.get("url", ""), i.get("description", ""))
            for i in items[:max_results]
        ]


class WebSearch:
    """Facade: builds the safe query, calls the provider, never raises."""

    def __init__(self, provider: SearchProvider | None, max_results: int = 3) -> None:
        self.provider = provider
        self.max_results = max_results

    def lookup(self, facts: ErrorFacts) -> SearchOutcome:
        if self.provider is None:
            return SearchOutcome(SearchState.DISABLED, None)
        query = build_query(facts)
        if query is None:
            return SearchOutcome(SearchState.DISABLED, None)
        try:
            results = self.provider.search(query, self.max_results)
        except Exception:
            return SearchOutcome(SearchState.UNAVAILABLE, query)
        safe = tuple(r for r in results if r.url.startswith(("https://", "http://")))
        return SearchOutcome(SearchState.OK, query, safe)


def build_search(provider: str, api_key: str | None, timeout: float, max_results: int) -> WebSearch:
    if provider == "none" or not api_key:
        return WebSearch(None, max_results)
    if provider == "tavily":
        return WebSearch(TavilySearch(api_key, timeout), max_results)
    if provider == "brave":
        return WebSearch(BraveSearch(api_key, timeout), max_results)
    raise ValueError(f"unknown search provider {provider!r}")
