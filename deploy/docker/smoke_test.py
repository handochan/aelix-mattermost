"""Smoke-test the hardened image against a local Mattermost fixture and a mock model.

Runs on the host with the standard library and the docker CLI:

    python deploy/docker/smoke_test.py [--image TAG] [--build] [--python PYTHON]

tests/mm_fixture.py needs aiohttp: pass --python (default: this interpreter) with one
that has it. Containers get the hardening of compose.yaml. Every container, the volume,
the fixture, the mock model and the temporary directory are removed at exit.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = "/etc/aelix-mattermost/config.toml"
# Keep in step with deploy/docker/compose.yaml.
HARDENING = (
    "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777,noexec,nosuid,nodev",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
    "--memory", "1g", "--pids-limit", "256", "--cpus", "2",
    "--add-host", "host.docker.internal:host-gateway",
)
CONFIG_TEMPLATE = """\
[mattermost]
url = "http://host.docker.internal:{mm_port}"
token_file = "/run/secrets/mattermost_token"
allowed_users = ["{user_id}"]
allow_insecure_http = true

[aelix]
command = ["aelix"]
model = "mock/mock-1"
offline = true
work_dir = "/var/lib/aelix-mattermost/workspace"
allowed_tools = []

[gateway]
state_dir = "/var/lib/aelix-mattermost/state"
max_concurrent_runs = 2
max_live_processes = 2
run_timeout = 120
rpc_timeout = 30
startup_timeout = 90
"""


class SmokeError(RuntimeError):
    pass


class ModelServer(ThreadingHTTPServer):
    """Minimal OpenAI-compatible streaming endpoint that always answers ``reply``."""

    daemon_threads = True

    def __init__(self, host: str, reply: str) -> None:
        super().__init__((host, 0), ModelHandler)
        self.reply = reply
        self.requests: list[str] = []


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ModelServer

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        self.server.requests.append(body.decode("utf-8", "replace"))
        base = {"id": "smoke", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-1"}
        choice = {"index": 0, "finish_reason": None}
        events = [
            {**base, "choices": [{**choice, "delta": {"role": "assistant", "content": ""}}]},
            {**base, "choices": [{**choice, "delta": {"content": self.server.reply}}]},
            {**base, "choices": [{**choice, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        payload = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


def docker(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise SmokeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result


def default_bind() -> str:
    """Host address that host.docker.internal (host-gateway) reaches from a container."""
    if platform.system() != "Linux":
        return "127.0.0.1"  # Docker Desktop and OrbStack forward it to host loopback.
    gateway = docker("network", "inspect", "bridge", "--format",
                     "{{(index .IPAM.Config 0).Gateway}}", check=False).stdout.strip()
    return gateway or "172.17.0.1"


def free_port(host: str) -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def ident() -> str:
    return secrets.token_hex(13)  # 26 characters, like a Mattermost ID


def wait_until(what: str, timeout: float, probe: Callable[[], bool], interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(interval)
    raise SmokeError(f"Timed out after {timeout:.0f}s waiting for {what}")


def find_message(value: object, marker: str) -> dict | None:
    """The innermost recorded object whose ``message`` contains ``marker``."""
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        if (found := find_message(child, marker)) is not None:
            return found
    if isinstance(value, dict) and isinstance(value.get("message"), str) and marker in value["message"]:
        return value
    return None


def recorded_reply(record: Path, marker: str) -> dict | None:
    if not record.is_file():
        return None
    for line in record.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            if (found := find_message(json.loads(line), marker)) is not None:
                return found
    return None


def dm_event(post_id: str, channel_id: str, user_id: str, text: str) -> dict:
    now = int(time.time() * 1000)
    post = {"id": post_id, "create_at": now, "update_at": now, "edit_at": 0, "delete_at": 0,
            "user_id": user_id, "channel_id": channel_id, "root_id": "", "original_id": "",
            "message": text, "type": "", "props": {}, "hashtags": "", "pending_post_id": ""}
    return {"event": "posted", "data": {"channel_type": "D", "post": json.dumps(post),
                                        "sender_name": "@smoke", "team_id": ""},
            "broadcast": {"omit_users": None, "user_id": "", "channel_id": channel_id, "team_id": ""}}


def step(name: str, result: subprocess.CompletedProcess[str], expect: str | None = None) -> str:
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or (expect is not None and expect not in output):
        raise SmokeError(f"{name} failed ({result.returncode}):\n{output[-3000:]}")
    print(f"ok   {name}: {output.splitlines()[-1] if output else ''}")
    return output


def container_state(name: str) -> dict:
    return json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)


class Smoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = secrets.token_hex(4)
        self.label = f"aelix-mattermost-smoke={self.run_id}"
        self.container = self.volume = f"aelix-mm-smoke-{self.run_id}"
        self.token = "smoke-" + secrets.token_hex(16)
        self.user_id, self.channel_id, self.post_id = ident(), ident(), ident()
        self.marker = f"smoke reply {secrets.token_hex(6)}"
        self.prompt = f"hello from the smoke test {secrets.token_hex(6)}"
        self.directory = Path(tempfile.mkdtemp(prefix="aelix-mm-smoke-"))
        self.fixture: subprocess.Popen[bytes] | None = None
        self.model: ModelServer | None = None
        self.runs = 0

    def docker_run(self, *args: str, detach: bool = False) -> subprocess.CompletedProcess[str]:
        self.runs += 1
        name = self.container if detach else f"{self.container}-{self.runs}"
        d = self.directory
        mounts = ("-v", f"{self.volume}:/var/lib/aelix-mattermost",
                  "-v", f"{d / 'config.toml'}:{CONFIG}:ro",
                  "-v", f"{d / 'models.json'}:/var/lib/aelix-mattermost/aelix-agent/models.json:ro",
                  "-v", f"{d / 'mattermost_token'}:/run/secrets/mattermost_token:ro")
        mode = ("-d",) if detach else ("--rm",)
        return docker("run", *mode, "--name", name, "--label", self.label, *HARDENING, *mounts,
                      *args, check=detach, timeout=300)

    def prepare(self) -> None:
        bind = self.args.bind or default_bind()
        self.model = ModelServer(bind, self.marker)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        mm_port, d = free_port(bind), self.directory
        models = {"providers": {"mock": {
            "baseUrl": f"http://host.docker.internal:{self.model.server_address[1]}/v1",
            "api": "openai-completions", "apiKey": "smoke-not-a-key",
            "models": [{"id": "mock-1", "name": "Mock", "reasoning": False, "input": ["text"],
                        "contextWindow": 128000, "maxTokens": 4096,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}
        event = dm_event(self.post_id, self.channel_id, self.user_id, self.prompt)
        files = {"config.toml": CONFIG_TEMPLATE.format(mm_port=mm_port, user_id=self.user_id),
                 "models.json": json.dumps(models), "mattermost_token": self.token + "\n",
                 "events.jsonl": json.dumps(event) + "\n"}
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
            (d / name).chmod(0o644)  # bind mounts keep host modes; uid 10001 must read them
        command = [self.args.python, str(self.args.fixture), "--host", bind, "--port", str(mm_port),
                   "--token", self.token, "--events", str(d / "events.jsonl"),
                   "--record", str(d / "record.jsonl")]
        with (d / "fixture.log").open("wb") as log:
            self.fixture = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)

        def listening() -> bool:
            if self.fixture is not None and self.fixture.poll() is not None:
                raise SmokeError(f"Mattermost fixture exited with {self.fixture.returncode}")
            with contextlib.suppress(OSError), socket.create_connection((bind, mm_port), 1):
                return True
            return False

        wait_until("the Mattermost fixture", 30, listening, 0.2)
        docker("volume", "create", "--label", self.label, self.volume)
        print(f"ok   fixture on {bind}:{mm_port}, mock model on port {self.model.server_address[1]}")

    def one_shot_commands(self) -> None:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        if step("--version", self.docker_run(self.args.image, "--version")).splitlines()[-1] != version:
            raise SmokeError(f"--version does not report {version}")
        step("check-config", self.docker_run(self.args.image, "check-config", "--config", CONFIG), "valid")
        step("doctor --check-aelix",
             self.docker_run(self.args.image, "doctor", "--config", CONFIG, "--check-aelix"))

    def gateway(self) -> None:
        self.docker_run("--health-interval", "3s", "--health-timeout", "10s", "--health-retries", "3",
                        self.args.image, detach=True)
        record = self.directory / "record.jsonl"
        reply: dict | None = None
        healthy = False

        def progressed() -> bool:
            nonlocal reply, healthy
            state = container_state(self.container)
            if not state.get("Running"):
                raise SmokeError(f"Gateway container stopped (exit {state.get('ExitCode')})")
            reply = reply or recorded_reply(record, self.marker)
            healthy = (state.get("Health") or {}).get("Status") == "healthy"
            return reply is not None and healthy

        try:
            wait_until("a bot reply and a healthy container", self.args.timeout, progressed)
        except SmokeError:
            print(f"reply recorded: {reply is not None}; healthy: {healthy}")
            raise
        assert reply is not None and self.model is not None
        if "root_id" in reply and reply["root_id"] != self.post_id:
            raise SmokeError("The bot reply is not threaded under the DM post")
        if not any(self.prompt in body for body in self.model.requests):
            raise SmokeError("The DM text never reached the model")
        print(f"ok   DM answered in the fixture record: {reply['message']!r}")
        print("ok   docker health: healthy")
        step("uid inside the gateway container", docker("exec", self.container, "id", "-u", check=False), "10001")
        probe = docker("exec", self.container, "touch", "/etc/aelix-mattermost/probe", check=False)
        if probe.returncode == 0 or "Read-only file system" not in probe.stderr:
            raise SmokeError(f"The root filesystem is writable: {probe.stderr.strip()}")
        print("ok   root filesystem is read-only")
        logs = docker("logs", self.container, check=False)
        if self.token in logs.stdout + logs.stderr:
            raise SmokeError("The bot token appeared in the gateway logs")
        docker("stop", "-t", "30", self.container, timeout=60)
        state = container_state(self.container)
        if state.get("ExitCode") != 0 or state.get("OOMKilled"):
            raise SmokeError(f"Gateway did not stop cleanly: exit {state.get('ExitCode')}, "
                             f"OOM killed {state.get('OOMKilled')}")
        print("ok   graceful stop (exit 0)")

    def diagnostics(self) -> None:
        if docker("container", "inspect", self.container, check=False).returncode == 0:
            state = docker("inspect", "--format", "{{json .State.Health}}", self.container, check=False)
            print("--- health ---\n" + state.stdout.strip()[-3000:])
            logs = docker("logs", "--tail", "80", self.container, check=False)
            print("--- gateway logs ---\n" + (logs.stdout + logs.stderr).strip()[-6000:])
        log = self.directory / "fixture.log"
        if log.is_file():
            print("--- fixture log ---\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])

    def cleanup(self) -> None:
        names = docker("ps", "-aq", "--filter", f"label={self.label}", check=False).stdout.split()
        if names:
            docker("rm", "-f", *names, check=False)
        docker("volume", "rm", "-f", self.volume, check=False)
        if self.fixture is not None and self.fixture.poll() is None:
            self.fixture.terminate()
            try:
                self.fixture.wait(10)
            except subprocess.TimeoutExpired:
                self.fixture.kill()
                self.fixture.wait()
        if self.model is not None:
            self.model.shutdown()
            self.model.server_close()
        shutil.rmtree(self.directory, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="aelix-mattermost:smoke", help="image tag to test")
    parser.add_argument("--build", action="store_true", help="build the image first (also when missing)")
    parser.add_argument("--python", default=sys.executable, help="interpreter with aiohttp for the fixture")
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests" / "mm_fixture.py")
    parser.add_argument("--bind", help="host address for the fixture and mock model (default: auto)")
    parser.add_argument("--timeout", type=float, default=180, help="seconds to wait for reply and health")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # keep "ok" lines ordered with FAIL on stderr
    if shutil.which("docker") is None or not args.fixture.is_file():
        print(f"FAIL needs the docker CLI and {args.fixture}", file=sys.stderr)
        return 1
    if args.build or docker("image", "inspect", args.image, check=False).returncode != 0:
        print(f"building {args.image} ...", flush=True)
        if subprocess.run(["docker", "build", "-t", args.image, str(ROOT)], check=False).returncode != 0:
            print("FAIL docker build", file=sys.stderr)
            return 1
    smoke = Smoke(args)
    try:
        smoke.prepare()
        smoke.one_shot_commands()
        smoke.gateway()
    except (SmokeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        smoke.diagnostics()
        return 1
    finally:
        smoke.cleanup()
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
