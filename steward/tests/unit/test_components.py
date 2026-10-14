"""Smaller components: heuristic baseline, search, HTTP reasoning client, CLI, embeddings."""

from __future__ import annotations

import json
import sys
import threading
import time
from types import ModuleType

import httpx
import pytest

from factories import REQUESTS, s1_request
from prayog_steward.cli import StewardClient, report_rebuild_failure
from prayog_steward.coordinator.validation import validate_response
from prayog_steward.rag.embed import HashingEmbedder, SentenceTransformerEmbedder, chunk, cosine
from prayog_steward.rag.sanitiser import ErrorFacts
from prayog_steward.reasoning_client import HttpReasoning
from prayog_steward.search import SearchResult, SearchState, WebSearch, build_query
from steward_contracts import Context, InvokeOutcome, Record, RecordKind, S1Input
from steward_runtime.heuristic import HeuristicReasoning
from steward_runtime.stub import StubReasoning

# ---------------------------------------------------------------- heuristic baseline


@pytest.mark.parametrize("ctx", list(Context))
def test_heuristic_output_always_passes_the_server_contract(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    v = validate_response(request, HeuristicReasoning().invoke(request))
    assert v.outcome is InvokeOutcome.OK and v.stripped == 0


def test_heuristic_classifies_oom_and_proposes_halving_the_batch() -> None:
    request = s1_request(
        Record(record_id="run:x", kind=RecordKind.RUN, body='{"job": {"batch_size": 256}}')
    )
    result = HeuristicReasoning().invoke(request).result
    assert result is not None and result["classification"] == "oom"
    assert result["proposal"]["changes"][0]["new_value"] == "128"


def test_heuristic_matches_only_earlier_failures_of_the_same_class() -> None:
    def earlier(run: str, end: str, previous: str | None) -> Record:
        body = json.dumps({"end": end, "previous_diagnosis": previous}, sort_keys=True)
        return Record(record_id=f"earlier:{run}", kind=RecordKind.EARLIER_RUN, body=body)

    request = s1_request(
        earlier("a", "FAILED", "oom"),
        earlier("b", "FAILED", "library_version"),
        earlier("c", "FAILED", None),
        earlier("d", "SUCCEEDED", "oom"),
    )
    result = HeuristicReasoning().invoke(request).result
    assert result is not None and result["matching_earlier_failures"] == ["earlier:a"]


def test_heuristic_never_cites_without_evidence() -> None:
    request = s1_request()
    payload = request.payload
    assert isinstance(payload, S1Input)
    empty = request.model_copy(update={"payload": payload.model_copy(update={"records": []})})
    result = HeuristicReasoning().invoke(empty).result
    assert result is not None and result["claims"] == [] and result["classification"] == "unknown"


# ---------------------------------------------------------------- search (ADR-S06)


def test_search_query_is_built_only_from_error_facts() -> None:
    facts = ErrorFacts("ModuleNotFoundError", None, (("timm", "0.9.2"), ("torch", "2.3.1")))
    assert build_query(facts) == "ModuleNotFoundError timm 0.9.2 torch 2.3.1 error"
    assert build_query(ErrorFacts(None, None, (("torch", "2.3.1"),))) is None


class Boom:
    name = "boom"

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        raise httpx.ConnectError("down")


def test_a20_search_failure_degrades_and_never_raises() -> None:
    facts = ErrorFacts("ValueError", None, ())
    assert WebSearch(Boom()).lookup(facts).state is SearchState.UNAVAILABLE
    assert WebSearch(None).lookup(facts).state is SearchState.DISABLED


# ---------------------------------------------------------------- HTTP reasoning client


def _client(handler: httpx.MockTransport) -> HttpReasoning:
    return HttpReasoning("http://runtime", client=httpx.Client(transport=handler))


def test_http_reasoning_round_trips_through_the_runtime_contract() -> None:
    request = s1_request()
    stub = StubReasoning()

    def handle(r: httpx.Request) -> httpx.Response:
        from steward_contracts import InvokeRequest

        return httpx.Response(
            200, content=stub.invoke(InvokeRequest.model_validate_json(r.content)).model_dump_json()
        )

    response = _client(httpx.MockTransport(handle)).invoke(request)
    assert response.outcome is InvokeOutcome.OK and response.invocation_id == "inv-1"


@pytest.mark.parametrize(
    "handler, outcome",
    [
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), InvokeOutcome.UNAVAILABLE),
        (lambda r: httpx.Response(503), InvokeOutcome.UNAVAILABLE),
        (lambda r: httpx.Response(200, content=b'{"nope": 1}'), InvokeOutcome.MALFORMED),
    ],
)
def test_nfr_gpu_055_runtime_problems_become_outcomes_not_exceptions(
    handler: object, outcome: InvokeOutcome
) -> None:
    client = _client(httpx.MockTransport(handler))  # type: ignore[arg-type]
    assert client.invoke(s1_request()).outcome is outcome


# ---------------------------------------------------------------- CLI (FR-STW-003, 004)


def _cli(handler: object) -> StewardClient:
    return StewardClient("http://api", "tok", httpx.Client(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]


def test_fr_stw_003_rebuild_failure_message_includes_link_and_ids() -> None:
    def handle(r: httpx.Request) -> httpx.Response:
        assert r.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"url": "https://prayog/steward/s/abc"})

    msg = report_rebuild_failure(_cli(handle), "a1", "snap1", "dependency error")
    assert "Attempt ID: a1" in msg and "Snapshot ID: snap1" in msg
    assert "Steward: available" in msg and "https://prayog/steward/s/abc" in msg


def test_a16_link_failure_gives_no_insecure_fallback() -> None:
    msg = report_rebuild_failure(
        _cli(lambda r: httpx.Response(503, json={"detail": "x"})), "a1", "snap1", "dependency error"
    )
    assert (
        "Steward: unavailable" in msg
        and "http" not in msg.split("Steward:")[1].split("prayog steward link")[0]
    )


# ---------------------------------------------------------------- embeddings and chunking


def test_hashing_embedder_ranks_lexically_related_text_higher() -> None:
    e = HashingEmbedder()
    q, near, far = e.embed(
        [
            "ModuleNotFoundError timm",
            "No module named timm.layers ModuleNotFoundError",
            "learning rate schedule warmup",
        ]
    )
    assert cosine(q, near) > cosine(q, far)
    assert abs(cosine(near, near) - 1.0) < 1e-9


def test_sentence_transformer_model_loads_once_under_concurrent_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str] = []

    class SlowModel:
        def __init__(self, name: str, device: str) -> None:
            time.sleep(0.2)  # wide window for a second loader to slip in
            loads.append(name)

        def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    fake = ModuleType("sentence_transformers")
    fake.SentenceTransformer = SlowModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    embedder = SentenceTransformerEmbedder("BAAI/bge-base-en-v1.5")
    threads = [threading.Thread(target=embedder.embed_query, args=(["q"],)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == ["BAAI/bge-base-en-v1.5"]


def test_chunking_respects_size_and_covers_all_lines() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    pieces = chunk(text, max_chars=300)
    assert all(len(p) <= 300 for p in pieces)
    assert "line 0 " in pieces[0] and "line 99 " in pieces[-1]
    assert chunk("") == []
