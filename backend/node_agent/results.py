"""
Sending a run's results to the server while it trains and when it ends.

The program writes into its output directory (see runners.py); the agent ships, once each:
    checkpoints/step-<N>[-best].<ext>   a checkpoint, as a WEIGHTS artifact at step N
    metrics.jsonl                       each complete line, as a metric
and, at the end, the program's log. Only regular files are read (a link or special file is skipped), size
is bounded, and a file still being written is left until it is stable. What has been shipped is kept in the
agent's state, so an agent restart never sends the same checkpoint or metric twice (the server also refuses
a repeated step).
"""

import json
import math
import os
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

CHECKPOINT = re.compile(r"^step-(\d{1,9})(-best)?(\.[A-Za-z0-9]{1,12})?$")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
STABLE_SECONDS = 2.0
LOG_UPLOAD_LIMIT = 5 * 1024 * 1024
LOG_TAIL_LIMIT = 64 * 1024
METRIC_BATCH = 500
BLOCK = 1024 * 1024


def _blocks(path: Path, limit: int | None = None) -> Iterator[bytes]:
    with path.open("rb") as source:
        remaining = limit
        while True:
            block = source.read(BLOCK if remaining is None else min(BLOCK, remaining))
            if not block:
                return
            if remaining is not None:
                remaining -= len(block)
            yield block


def _regular(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


class ResultShipper:
    """`state` is a JSON-able dict the agent persists: {"checkpoints": [names], "metrics_offset": int}."""

    def __init__(self, http: httpx.Client, headers: dict, allocation_id: int, output_dir: Path, state: dict,
                 clock=time.time):
        self.http, self.headers, self.allocation_id = http, headers, allocation_id
        self.output_dir, self.state, self.clock = output_dir, state, clock
        state.setdefault("checkpoints", [])
        state.setdefault("metrics_offset", 0)
        state.setdefault("skipped", [])

    def _url(self, what: str) -> str:
        return f"/agent/allocations/{self.allocation_id}/{what}"

    def ship(self, final: bool = False) -> None:
        """Send what is new. Raises httpx.HTTPError if the server cannot be reached (retry next step)."""
        self._ship_metrics()
        self._ship_checkpoints(final)

    def _ship_metrics(self) -> None:
        path = self.output_dir / "metrics.jsonl"
        if _regular(path) is None:
            return
        with path.open("rb") as source:
            source.seek(self.state["metrics_offset"])
            data = source.read(16 * BLOCK)
        consumed, batch = 0, []
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break                                            # a half-written line: wait for the rest
            consumed += len(line)
            try:
                item = json.loads(line)
                name, value, step = item["name"], float(item["value"]), int(item.get("step", 0))
                if isinstance(name, str) and name.strip() and math.isfinite(value) and step >= 0:
                    batch.append({"name": name.strip()[:100], "value": value, "step": step})
            except (ValueError, KeyError, TypeError):
                continue                                         # skip a bad line; it does not stop the rest
            if len(batch) == METRIC_BATCH:
                break
        if batch:
            response = self.http.post(self._url("metrics"), json={"metrics": batch}, headers=self.headers)
            if response.status_code >= 500:
                raise httpx.HTTPError(f"metrics: HTTP {response.status_code}")
            # 201 recorded; 409 a step already recorded (a repeat after a restart); anything else is dropped
        self.state["metrics_offset"] += consumed

    def _ship_checkpoints(self, final: bool) -> None:
        directory = self.output_dir / "checkpoints"
        if not directory.is_dir():
            return
        found = []
        for path in directory.iterdir():
            match = CHECKPOINT.match(path.name)
            info = _regular(path)
            if (not match or path.suffix == ".tmp" or info is None
                    or path.name in self.state["checkpoints"] or path.name in self.state["skipped"]):
                continue                                         # a *.tmp is still being written
            found.append((int(match.group(1)), path, info, bool(match.group(2))))
        for step, path, info, best in sorted(found, key=lambda item: item[0]):
            if info.st_size == 0 or info.st_size > MAX_UPLOAD_BYTES:
                self.state["skipped"].append(path.name)          # nothing to send, or more than a checkpoint may be
                continue
            if not final and self.clock() - info.st_mtime < STABLE_SECONDS:
                continue                                         # still being written
            response = self.http.post(self._url("results"), params={"kind": "WEIGHTS", "step": step,
                                      "best": str(best).lower()}, content=_blocks(path), timeout=600,
                                      headers={**self.headers, "Content-Type": "application/octet-stream"})
            if response.status_code >= 500:
                raise httpx.HTTPError(f"checkpoint {path.name}: HTTP {response.status_code}")
            self.state["checkpoints"].append(path.name)          # sent, or the server has that step already

    def ship_log(self, log_path: Path) -> str:
        """Upload the program's log as the run's LOG artifact; returns the last bytes of it for the report."""
        info = _regular(log_path)
        if info is None or info.st_size == 0:
            return ""
        size = info.st_size
        with log_path.open("rb") as source:
            source.seek(max(0, size - LOG_TAIL_LIMIT))
            tail = source.read().decode("utf-8", "replace")
        response = self.http.post(self._url("results"), params={"kind": "LOG"}, content=_blocks(log_path, LOG_UPLOAD_LIMIT),
                                  timeout=600, headers={**self.headers, "Content-Type": "application/octet-stream"})
        if response.status_code >= 500:
            raise httpx.HTTPError(f"log: HTTP {response.status_code}")
        return tail
