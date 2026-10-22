"""
Starting, watching and stopping a run's program.

    DockerRunner      production. The program runs in a container with no network, a read-only root, no
                      added capabilities, a non-root user, limits on processes, memory and CPUs, and only the
                      GPUs the scheduler allocated. The code and data are mounted read-only; the program's
                      output directory is the only place it can write.
    SubprocessRunner  development and tests only: a plain child process with NO isolation. Never use it for
                      other people's code.

The program's contract, the same under both: it is started as `python <entrypoint>` in the code directory,
and finds its inputs and outputs through the environment:

    PRAYOG_HYPERPARAMETERS_JSON  the run's hyperparameters
    PRAYOG_DATA_DIR              the dataset, read-only (files at the paths the user's dataset had)
    PRAYOG_OUTPUT_DIR            where to write results:
        checkpoints/step-<N>.<ext>        a checkpoint at step N (write to a *.tmp name, then rename)
        checkpoints/step-<N>-best.<ext>   a checkpoint that is the best so far
        metrics.jsonl                     one JSON object per line: {"name": ..., "value": ..., "step": ...}
    stdout and stderr                      become the run's log

A handle is a small JSON object so the agent can persist it and find the program again after a restart.
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STOP_GRACE_SECONDS = 20


@dataclass(frozen=True)
class RunSpec:
    name: str                        # unique, e.g. "alloc-12"
    run_id: int
    label: str
    entrypoint: str
    hyperparameters_json: str
    code_dir: Path
    data_dir: Path
    output_dir: Path
    log_path: Path
    gpu_indexes: tuple[int, ...]


def environment(spec: RunSpec, data: str, output: str) -> dict[str, str]:
    return {
        "PRAYOG_RUN_ID": str(spec.run_id),
        "PRAYOG_RUN_LABEL": spec.label,
        "PRAYOG_HYPERPARAMETERS_JSON": spec.hyperparameters_json,
        "PRAYOG_DATA_DIR": data,
        "PRAYOG_OUTPUT_DIR": output,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class SubprocessRunner:
    """No isolation. For tests and for a developer's own machine."""

    def __init__(self):
        self._children: dict[str, subprocess.Popen] = {}

    def start(self, spec: RunSpec) -> dict:
        env = {**os.environ, **environment(spec, str(spec.data_dir), str(spec.output_dir)),
               "CUDA_VISIBLE_DEVICES": ",".join(map(str, spec.gpu_indexes))}
        log = spec.log_path.open("ab")
        child = subprocess.Popen([sys.executable, "-u", spec.entrypoint], cwd=spec.code_dir, env=env,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self._children[spec.name] = child
        return {"kind": "subprocess", "name": spec.name, "pid": child.pid}

    def poll(self, handle: dict) -> int | None:
        child = self._children.get(handle["name"])
        if child is None:                                       # the agent restarted: the child is not ours any more
            return 137
        code = child.poll()
        return None if code is None else (code if code >= 0 else 128 - code)

    def stop(self, handle: dict) -> None:
        child = self._children.get(handle["name"])
        if child is None or child.poll() is not None:
            return
        os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def cleanup(self, handle: dict) -> None:
        self._children.pop(handle["name"], None)


class DockerRunner:
    def __init__(self, image: str, memory: str = "16g", cpus: str = "8", pids: int = 512, docker: str = "docker",
                 expose_gpus: bool = True):
        """expose_gpus=False is for a node or a developer machine with no NVIDIA runtime: the container then
        sees no GPU at all (the allocation's units are still reserved in the scheduler)."""
        self.image, self.memory, self.cpus, self.pids, self.docker = image, memory, cpus, pids, docker
        self.expose_gpus = expose_gpus

    def command(self, spec: RunSpec) -> list[str]:
        env = environment(spec, "/data", "/output")
        command = [
            self.docker, "run", "--detach", "--pull=never", "--name", f"prayog-{spec.name}",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids), "--memory", self.memory, "--cpus", self.cpus,
            "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=1g", "--ulimit", "nofile=4096:4096",
            "--stop-timeout", str(STOP_GRACE_SECONDS),
            "--mount", f"type=bind,source={spec.code_dir},target=/workspace,readonly",
            "--mount", f"type=bind,source={spec.data_dir},target=/data,readonly",
            "--mount", f"type=bind,source={spec.output_dir},target=/output",
            "--workdir", "/workspace", "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
        ]
        if spec.gpu_indexes and self.expose_gpus:
            command += ["--gpus", '"device=' + ",".join(map(str, spec.gpu_indexes)) + '"']
        for key, value in env.items():
            command += ["--env", f"{key}={value}"]
        return command + [self.image, "python", "-u", spec.entrypoint]

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=60)

    def start(self, spec: RunSpec) -> dict:
        result = subprocess.run(self.command(spec), capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError("Docker could not start the container: " + result.stderr.strip()[-500:])
        return {"kind": "docker", "name": f"prayog-{spec.name}", "log": str(spec.log_path)}

    def poll(self, handle: dict) -> int | None:
        result = self._run("inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", handle["name"])
        if result.returncode != 0:
            return 137                                           # the container vanished
        status, code = result.stdout.split()
        return None if status in ("created", "running", "restarting", "paused") else int(code)

    def stop(self, handle: dict) -> None:
        self._run("stop", handle["name"])

    def cleanup(self, handle: dict) -> None:
        logs = self._run("logs", handle["name"])
        with Path(handle["log"]).open("ab") as out:
            out.write((logs.stdout + logs.stderr).encode("utf-8", "replace"))
        self._run("rm", "--force", handle["name"])
