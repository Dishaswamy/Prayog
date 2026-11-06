import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from blake3 import blake3

from node_agent import results, runners, workspace
from node_agent.results import ResultShipper
from node_agent.runners import DockerRunner, RunSpec, SubprocessRunner
from node_agent.workspace import WorkspaceError

HEADERS = {"X-Node-Credential": "cred"}


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def make_archive(files: dict[str, bytes], extra=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
        for info, body in extra or []:
            archive.writestr(info, body)
    return buffer.getvalue()


def inventory(files: dict[str, bytes]) -> list[dict]:
    return [{"path": p, "size_bytes": len(b), "sha256": sha(b), "role": "source", "included_in_archive": True}
            for p, b in sorted(files.items())]


class FakeServer:
    """Serves chunks and data by id; records what was asked."""

    def __init__(self, objects: dict[str, bytes], status=200):
        self.objects, self.status, self.asked = objects, status, []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.asked.append(request.url.path)
            key = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(self.status if key in self.objects else 404, content=self.objects.get(key, b""))
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://server")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.files = {"main.py": b"print(1)\n", "pkg/mod.py": b"X = 1\n"}

    def load(self, archive: bytes | None = None, chunk_size=64, files=None, **over) -> tuple[dict, dict]:
        archive = archive if archive is not None else make_archive(self.files)
        pieces = [archive[i:i + chunk_size] for i in range(0, len(archive), chunk_size)]
        objects = {f"c{n}": piece for n, piece in enumerate(pieces)}
        load = {"entrypoint": "main.py", "hyperparameters": {}, "dataset": [], "files": files or inventory(self.files),
                "archive_sha256": sha(archive), "archive_size_bytes": len(archive),
                "chunks": [{"artifact_id": f"c{n}", "sha256": sha(p), "size_bytes": len(p)} for n, p in enumerate(pieces)],
                **over}
        return load, objects

    def prepare(self, load, objects):
        self.attempts = getattr(self, "attempts", 0) + 1                      # a fresh workspace each time, as in use
        return workspace.prepare(FakeServer(objects).client(), HEADERS, 7, load, self.root / f"ws{self.attempts}")

    def test_good_code_is_joined_verified_and_extracted_read_only(self):
        load, objects = self.load()
        dirs = self.prepare(load, objects)
        self.assertEqual((dirs["code"] / "pkg" / "mod.py").read_bytes(), b"X = 1\n")
        self.assertEqual(os.stat(dirs["code"] / "main.py").st_mode & 0o222, 0)
        self.assertEqual(oct(os.stat(dirs["output"]).st_mode & 0o777), "0o777")

    def test_a_chunk_that_fails_its_checksum_is_refused(self):
        load, objects = self.load()
        objects["c0"] = objects["c0"][:-1] + b"X"
        with self.assertRaisesRegex(WorkspaceError, "chunk failed its checksum"):
            self.prepare(load, objects)

    def test_a_chunk_of_the_wrong_length_is_refused(self):
        load, objects = self.load()
        objects["c0"] += b"extra"
        with self.assertRaisesRegex(WorkspaceError, "more bytes than recorded"):
            self.prepare(load, objects)
        load, objects = self.load()
        objects["c0"] = objects["c0"][:-3]
        with self.assertRaisesRegex(WorkspaceError, "were recorded"):
            self.prepare(load, objects)

    def test_an_archive_that_fails_the_manifest_digest_is_refused(self):
        load, objects = self.load()
        load["archive_sha256"] = "0" * 64
        with self.assertRaisesRegex(WorkspaceError, "archive failed its checksum"):
            self.prepare(load, objects)

    def test_a_server_refusal_is_reported(self):
        load, _ = self.load()
        with self.assertRaisesRegex(WorkspaceError, "refused"):
            self.prepare(load, {})

    def test_a_file_that_differs_from_the_inventory_is_refused(self):
        archive = make_archive({"main.py": b"print(2)\n", "pkg/mod.py": b"X = 1\n"})
        load, objects = self.load(archive=archive)               # inventory still says print(1)
        with self.assertRaisesRegex(WorkspaceError, "code file failed its checksum"):
            self.prepare(load, objects)

    def test_a_file_the_manifest_does_not_list_is_refused(self):
        archive = make_archive({**self.files, "sneaky.py": b"evil\n"})
        load, objects = self.load(archive=archive)
        with self.assertRaisesRegex(WorkspaceError, "unexpected file"):
            self.prepare(load, objects)

    def test_a_listed_file_that_is_missing_is_refused(self):
        archive = make_archive({"main.py": b"print(1)\n"})
        load, objects = self.load(archive=archive)
        with self.assertRaisesRegex(WorkspaceError, "missing files"):
            self.prepare(load, objects)

    def test_path_traversal_in_the_archive_is_refused(self):
        archive = make_archive({"main.py": b"print(1)\n"}, extra=[("../escape.py", b"x")])
        load, objects = self.load(archive=archive)
        with self.assertRaisesRegex(WorkspaceError, "Unsafe path"):
            self.prepare(load, objects)
        self.assertFalse((self.root / "escape.py").exists())

    def test_a_symlink_in_the_archive_is_refused(self):
        link = zipfile.ZipInfo("link.py")
        link.external_attr = (0o120777) << 16
        archive = make_archive({"main.py": b"print(1)\n"}, extra=[(link, b"/etc/passwd")])
        load, objects = self.load(archive=archive)
        with self.assertRaisesRegex(WorkspaceError, "link or special"):
            self.prepare(load, objects)

    def test_an_entrypoint_outside_the_code_is_refused(self):
        load, objects = self.load(entrypoint="missing.py")
        with self.assertRaisesRegex(WorkspaceError, "not in the code"):
            self.prepare(load, objects)
        for bad in ("../main.py", "/main.py", "a/../../x.py"):
            load, objects = self.load(entrypoint=bad)
            with self.subTest(bad=bad), self.assertRaisesRegex(WorkspaceError, "Unsafe path"):
                self.prepare(load, objects)

    # ── data ──────────────────────────────────────────────────────────────

    def data_load(self, payload: bytes, path="data/train.csv", **over):
        item = {"path": path, "size_bytes": len(payload), "blake3": blake3(payload).hexdigest(), **over}
        load, objects = self.load(dataset=[item])
        objects[item["blake3"]] = payload
        return load, objects

    def test_dataset_files_are_placed_at_their_paths_read_only(self):
        load, objects = self.data_load(b"a,b\n1,2\n")
        dirs = self.prepare(load, objects)
        target = dirs["data"] / "data" / "train.csv"
        self.assertEqual(target.read_bytes(), b"a,b\n1,2\n")
        self.assertEqual(os.stat(target).st_mode & 0o222, 0)

    def test_dataset_bytes_that_fail_the_blake3_check_are_refused_and_removed(self):
        load, objects = self.data_load(b"a,b\n1,2\n")
        objects[load["dataset"][0]["blake3"]] = b"a,b\n9,9\n"
        with self.assertRaisesRegex(WorkspaceError, "BLAKE3"):
            self.prepare(load, objects)
        self.assertFalse((self.root / "ws1" / "data" / "data" / "train.csv").exists())

    def test_dataset_of_the_wrong_size_is_refused(self):
        load, objects = self.data_load(b"a,b\n1,2\n", size_bytes=3)
        with self.assertRaisesRegex(WorkspaceError, "more bytes than recorded"):
            self.prepare(load, objects)

    def test_unsafe_dataset_paths_are_refused(self):
        for bad in ("../x.csv", "/abs.csv", "a/../../b.csv"):
            load, objects = self.data_load(b"d", path=bad)
            with self.subTest(bad=bad), self.assertRaisesRegex(WorkspaceError, "Unsafe path"):
                self.prepare(load, objects)


class RunnerTests(unittest.TestCase):
    def spec(self, **over) -> RunSpec:
        base = Path("/tmp/prayog-test")
        values = dict(name="alloc-9", run_id=4, label="A run 1", entrypoint="main.py", hyperparameters_json='{"lr":0.1}',
                      code_dir=base / "code", data_dir=base / "data", output_dir=base / "output",
                      log_path=base / "run.log", gpu_indexes=(0, 1))
        values.update(over)
        return RunSpec(**values)

    def test_the_container_is_locked_down(self):
        command = DockerRunner("img:1").command(self.spec())
        joined = " ".join(command)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--user 65534:65534", "--pull=never", "--pids-limit", "--memory", "--cpus"):
            self.assertIn(flag, joined)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("host", joined.replace("source=", ""))             # no host network, pid or ipc namespaces

    def test_code_and_data_are_read_only_and_only_the_output_is_writable(self):
        mounts = [part for part in DockerRunner("img:1").command(self.spec()) if part.startswith("type=bind")]
        by_target = {m.split("target=")[1].split(",")[0]: m for m in mounts}
        self.assertEqual(set(by_target), {"/workspace", "/data", "/output"})
        self.assertTrue(by_target["/workspace"].endswith("readonly"))
        self.assertTrue(by_target["/data"].endswith("readonly"))
        self.assertFalse(by_target["/output"].endswith("readonly"))

    def test_only_the_allocated_gpus_are_exposed(self):
        command = DockerRunner("img:1").command(self.spec(gpu_indexes=(2, 3)))
        self.assertEqual(command[command.index("--gpus") + 1], '"device=2,3"')
        self.assertNotIn("--gpus", DockerRunner("img:1").command(self.spec(gpu_indexes=())))

    def test_a_machine_without_gpu_passthrough_exposes_none(self):
        self.assertNotIn("--gpus", DockerRunner("img:1", expose_gpus=False).command(self.spec(gpu_indexes=(0, 1))))

    def test_the_program_finds_its_inputs_through_the_environment(self):
        command = DockerRunner("img:1").command(self.spec())
        env = dict(part.split("=", 1) for flag, part in zip(command, command[1:]) if flag == "--env")
        self.assertEqual((env["PRAYOG_DATA_DIR"], env["PRAYOG_OUTPUT_DIR"]), ("/data", "/output"))
        self.assertEqual(env["PRAYOG_HYPERPARAMETERS_JSON"], '{"lr":0.1}')
        self.assertEqual((env["PRAYOG_RUN_ID"], env["PRAYOG_RUN_LABEL"]), ("4", "A run 1"))
        self.assertEqual(command[-4:], ["img:1", "python", "-u", "main.py"])

    def test_the_container_is_named_for_the_allocation(self):
        command = DockerRunner("img:1").command(self.spec())
        self.assertEqual(command[command.index("--name") + 1], "prayog-alloc-9")

    def test_docker_state_maps_to_an_exit_code(self):
        runner = DockerRunner("img:1")
        for stdout, expected in (("running 0\n", None), ("created 0\n", None), ("paused 0\n", None),
                                 ("exited 0\n", 0), ("exited 3\n", 3), ("dead 137\n", 137)):
            with self.subTest(stdout=stdout), patch.object(runners.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=stdout, stderr="")):
                self.assertEqual(runner.poll({"name": "c"}), expected)

    def test_a_vanished_container_counts_as_killed(self):
        with patch.object(runners.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="no such")):
            self.assertEqual(DockerRunner("img:1").poll({"name": "c"}), 137)

    def test_a_container_that_will_not_start_is_an_error(self):
        with patch.object(runners.subprocess, "run", return_value=subprocess.CompletedProcess([], 125, stdout="", stderr="no such image")):
            with self.assertRaisesRegex(RuntimeError, "no such image"):
                DockerRunner("img:1").start(self.spec())

    def test_cleanup_keeps_the_containers_output_in_the_log_then_removes_it(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "run.log"
            calls = []

            def fake(args, **kw):
                calls.append(args[1])
                return subprocess.CompletedProcess(args, 0, stdout="line one\n", stderr="warn\n")
            with patch.object(runners.subprocess, "run", side_effect=fake):
                DockerRunner("img:1").cleanup({"name": "c", "log": str(log)})
            self.assertEqual(calls, ["logs", "rm"])
            self.assertEqual(log.read_text(), "line one\nwarn\n")


class SubprocessRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("code", "data", "output"):
            (self.root / name).mkdir()

    def start(self, source: str, runner=None, **over):
        (self.root / "code" / "main.py").write_text(source)
        runner = runner or SubprocessRunner()
        spec = RunSpec(name="a", run_id=1, label="A run 1", entrypoint="main.py", hyperparameters_json="{}",
                       code_dir=self.root / "code", data_dir=self.root / "data", output_dir=self.root / "output",
                       log_path=self.root / "run.log", gpu_indexes=over.get("gpus", (1, 3)))
        return runner, runner.start(spec)

    def wait(self, runner, handle, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code = runner.poll(handle)
            if code is not None:
                return code
            time.sleep(0.02)
        self.fail("the program did not finish")

    def test_exit_codes_and_output_are_reported(self):
        runner, handle = self.start("import sys; print('hello'); sys.exit(3)")
        self.assertEqual(self.wait(runner, handle), 3)
        self.assertIn("hello", (self.root / "run.log").read_text())

    def test_the_program_sees_its_environment_and_only_its_gpus(self):
        source = "import os; print(os.environ['CUDA_VISIBLE_DEVICES'], os.environ['PRAYOG_OUTPUT_DIR'])"
        runner, handle = self.start(source)
        self.wait(runner, handle)
        self.assertEqual((self.root / "run.log").read_text().split(), ["1,3", str(self.root / "output")])

    def test_stop_ends_a_program_that_ignores_nothing(self):
        runner, handle = self.start("import time; time.sleep(600)")
        self.assertIsNone(runner.poll(handle))
        runner.stop(handle)
        self.assertIsNotNone(runner.poll(handle))

    def test_stop_kills_a_program_that_ignores_sigterm(self):
        with patch.object(runners, "STOP_GRACE_SECONDS", 0.3):
            runner, handle = self.start("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(600)")
            time.sleep(0.5)
            runner.stop(handle)
        self.assertIsNotNone(runner.poll(handle))

    def test_a_program_the_agent_lost_track_of_counts_as_killed(self):
        self.assertEqual(SubprocessRunner().poll({"name": "never-started"}), 137)


class ResultShipperTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        (self.out / "checkpoints").mkdir()
        self.sent, self.status = [], {"results": 201, "metrics": 201}
        self.state = {}

    def shipper(self, state=None, clock=lambda: time.time() + 100):
        def handler(request: httpx.Request) -> httpx.Response:
            kind = "metrics" if request.url.path.endswith("/metrics") else "results"
            self.sent.append((kind, dict(request.url.params), request.content))
            return httpx.Response(self.status[kind], json={})
        http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://server")
        return ResultShipper(http, HEADERS, 5, self.out, self.state if state is None else state, clock=clock)

    def checkpoint(self, name: str, body: bytes = b"w") -> Path:
        path = self.out / "checkpoints" / name
        path.write_bytes(body)
        return path

    def uploads(self):
        return [(p["step"], p["best"], c) for kind, p, c in self.sent if kind == "results"]

    def test_checkpoints_are_sent_in_step_order_once_each(self):
        self.checkpoint("step-10.bin", b"ten")
        self.checkpoint("step-2.bin", b"two")
        self.checkpoint("step-3-best.pt", b"three")
        shipper = self.shipper()
        shipper.ship()
        shipper.ship()
        self.assertEqual(self.uploads(), [("2", "false", b"two"), ("3", "true", b"three"), ("10", "false", b"ten")])

    def test_what_was_sent_is_remembered_across_an_agent_restart(self):
        self.checkpoint("step-1.bin")
        self.shipper().ship()
        self.shipper(state=json.loads(json.dumps(self.state))).ship()
        self.assertEqual(len(self.uploads()), 1)

    def test_names_that_are_not_checkpoints_are_ignored(self):
        for name in ("step-1.tmp", "step-x.bin", "model.bin", "step-1.bin.bak", "step-.bin", "step-1234567890.bin"):
            self.checkpoint(name)
        self.shipper().ship()
        self.assertEqual(self.uploads(), [])

    def test_a_symlink_a_directory_and_an_empty_file_are_not_sent(self):
        target = self.out / "secret.txt"
        target.write_text("do not send")
        (self.out / "checkpoints" / "step-1.bin").symlink_to(target)
        (self.out / "checkpoints" / "step-2.bin").mkdir()
        self.checkpoint("step-3.bin", b"")
        self.shipper().ship()
        self.assertEqual(self.uploads(), [])

    def test_an_oversized_checkpoint_is_skipped_for_good(self):
        self.checkpoint("step-1.bin", b"0123456789")
        with patch.object(results, "MAX_UPLOAD_BYTES", 5):
            shipper = self.shipper()
            shipper.ship()
            shipper.ship()
        self.assertEqual(self.uploads(), [])
        self.assertEqual(self.state["skipped"], ["step-1.bin"])

    def test_a_file_still_changing_waits_until_stable_unless_final(self):
        self.checkpoint("step-1.bin")
        young = self.shipper(clock=lambda: time.time())
        young.ship()
        self.assertEqual(self.uploads(), [])
        young.ship(final=True)
        self.assertEqual(len(self.uploads()), 1)

    def test_a_server_error_leaves_the_checkpoint_to_be_retried(self):
        self.checkpoint("step-1.bin")
        self.status["results"] = 503
        with self.assertRaises(httpx.HTTPError):
            self.shipper().ship()
        self.assertNotIn("step-1.bin", self.state.get("checkpoints", []))
        self.status["results"] = 201
        self.shipper().ship()
        self.assertEqual(len(self.uploads()), 2)

    def test_a_step_the_server_already_has_is_not_retried(self):
        self.checkpoint("step-1.bin")
        self.status["results"] = 409
        shipper = self.shipper()
        shipper.ship()
        shipper.ship()
        self.assertEqual(len(self.uploads()), 1)

    # ── metrics ───────────────────────────────────────────────────────────

    def metrics_sent(self):
        return [json.loads(c)["metrics"] for kind, _, c in self.sent if kind == "metrics"]

    def write_metrics(self, text: str, mode="w"):
        with open(self.out / "metrics.jsonl", mode) as f:
            f.write(text)

    def test_metrics_lines_are_sent_as_a_batch(self):
        self.write_metrics('{"name": "loss", "value": 0.5, "step": 1}\n{"name": "acc", "value": 0.9}\n')
        self.shipper().ship()
        self.assertEqual(self.metrics_sent(), [[{"name": "loss", "value": 0.5, "step": 1},
                                                {"name": "acc", "value": 0.9, "step": 0}]])

    def test_a_half_written_line_waits_for_the_rest(self):
        self.write_metrics('{"name": "loss", "value": 0.5, "step": 1}\n{"name": "lo')
        shipper = self.shipper()
        shipper.ship()
        self.write_metrics('ss", "value": 0.4, "step": 2}\n', mode="a")
        shipper.ship()
        self.assertEqual([[m["step"] for m in batch] for batch in self.metrics_sent()], [[1], [2]])

    def test_bad_lines_are_skipped_without_blocking_the_rest(self):
        self.write_metrics('not json\n{"name": "ok", "value": 1}\n{"value": 2}\n{"name": "nan", "value": NaN}\n'
                           '{"name": "inf", "value": Infinity}\n{"name": "neg", "value": 1, "step": -1}\n'
                           '{"name": "", "value": 1}\n[1,2]\n')
        self.shipper().ship()
        self.assertEqual(self.metrics_sent(), [[{"name": "ok", "value": 1.0, "step": 0}]])

    def test_metrics_are_not_sent_twice_even_after_a_restart(self):
        self.write_metrics('{"name": "loss", "value": 1, "step": 1}\n')
        self.shipper().ship()
        self.shipper(state=json.loads(json.dumps(self.state))).ship()
        self.assertEqual(len(self.metrics_sent()), 1)

    def test_a_server_error_does_not_lose_metrics(self):
        self.write_metrics('{"name": "loss", "value": 1, "step": 1}\n')
        self.status["metrics"] = 500
        with self.assertRaises(httpx.HTTPError):
            self.shipper().ship()
        self.status["metrics"] = 201
        self.shipper().ship()
        self.assertEqual(len(self.metrics_sent()), 2)                          # the second attempt landed

    def test_a_large_metrics_file_is_sent_in_bounded_batches(self):
        self.write_metrics("".join(json.dumps({"name": "m", "value": i, "step": i}) + "\n" for i in range(1200)))
        shipper = self.shipper()
        for _ in range(3):
            shipper.ship()
        self.assertEqual([len(b) for b in self.metrics_sent()], [500, 500, 200])

    def test_a_metrics_symlink_is_not_followed(self):
        target = self.out / "elsewhere.txt"
        target.write_text('{"name": "stolen", "value": 1}\n')
        (self.out / "metrics.jsonl").symlink_to(target)
        self.shipper().ship()
        self.assertEqual(self.metrics_sent(), [])

    # ── log ───────────────────────────────────────────────────────────────

    def test_the_log_is_uploaded_and_its_tail_returned(self):
        log = self.out / "run.log"
        log.write_text("x" * 100000 + "THE END")
        tail = self.shipper().ship_log(log)
        self.assertTrue(tail.endswith("THE END"))
        self.assertLessEqual(len(tail), results.LOG_TAIL_LIMIT)
        kind, params, body = self.sent[-1]
        self.assertEqual((kind, params), ("results", {"kind": "LOG"}))
        self.assertEqual(len(body), 100007)

    def test_an_empty_or_missing_log_sends_nothing(self):
        self.assertEqual(self.shipper().ship_log(self.out / "nope.log"), "")
        (self.out / "empty.log").write_text("")
        self.assertEqual(self.shipper().ship_log(self.out / "empty.log"), "")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
