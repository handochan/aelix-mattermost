import configparser
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

# A double that follows the real server's REST and WebSocket rules.
from mm_fixture import MattermostFixture

from aelix_mattermost import cli
from aelix_mattermost.config import Config, ConfigError, load_config
from aelix_mattermost.mattermost import AuthenticationError, MattermostError
from aelix_mattermost.rpc import RpcError, RpcProcess

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).with_name("fake_aelix.py").resolve()
UNIT = ROOT / "deploy" / "aelix-mattermost.service"
CONFIG = """\
[mattermost]
url = "https://chat.example.internal"
allowed_users = ["u1"]

[gateway]
state_dir = "state"
"""


def files(root: Path) -> dict[str, float]:
    return {str(p.relative_to(root)): p.stat().st_mtime for p in root.rglob("*")}


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(CONFIG, encoding="utf-8")

    def write_health(self, age=1.0, connected=True, **changes):
        now = time.time()
        data = {"version": 1, "pid": 4242, "updated_at": now - age, "websocket_connected": connected,
                "connected_since": now - 90 if connected else None, "last_event_at": now - 5, **changes}
        (self.state / "health.json").write_text(json.dumps(data), encoding="utf-8")

    def healthcheck(self, *args):
        """main() in-process; returns (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(["healthcheck", "--config", str(self.config), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_unhealthy(self, reason, *args):
        code, out, err = self.healthcheck(*args)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("unhealthy: "), err)
        self.assertIn(reason, err)
        self.assertEqual(err.count("\n"), 1)  # one line

    def test_fresh_and_connected_is_healthy(self):
        self.write_health(age=2)
        code, out, err = self.healthcheck()
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^healthy: Mattermost WebSocket connected for 9\ds; "
                              r"health.json written [23]s ago\n$")

    def test_stale_health_is_unhealthy(self):
        self.write_health(age=61)
        self.assert_unhealthy("stale (written 61s ago, limit 60s)")
        self.assertEqual(self.healthcheck("--max-age", "120")[0], 0)
        self.write_health(age=10)
        self.assert_unhealthy("stale", "--max-age", "5")
        self.write_health(age=-3600)  # the clock went back: a dead gateway must not look fresh
        self.assert_unhealthy("in the future")

    def test_disconnected_websocket_is_unhealthy(self):
        self.write_health(connected=False)
        self.assert_unhealthy("WebSocket is disconnected")

    def test_missing_health_file_is_unhealthy(self):
        self.assert_unhealthy("does not exist")
        (self.state / "health.json").mkdir()  # unreadable as a file
        self.assert_unhealthy("cannot read")

    def test_malformed_health_files_are_unhealthy(self):
        (self.state / "health.json").write_text("{not json", encoding="utf-8")
        self.assert_unhealthy("not valid JSON")
        for changes in ({"version": 2}, {"updated_at": None}, {"updated_at": "now"}):
            with self.subTest(changes=changes):
                self.write_health(**changes)
                self.assert_unhealthy("unsupported format")
        (self.state / "health.json").write_text("[]", encoding="utf-8")
        self.assert_unhealthy("unsupported format")

    def test_unreadable_configuration_is_unhealthy(self):
        self.config.write_text("[gateway]\nstate_dir = 3\n", encoding="utf-8")
        self.assert_unhealthy("state_dir must be a path string")
        self.config.unlink()
        self.assert_unhealthy("Cannot read TOML configuration")

    def test_needs_no_token_no_network_and_no_writes(self):
        self.write_health()
        before = files(self.root)
        if os.name == "posix":  # a read-only filesystem, as in the hardened container
            for path in (self.state, self.root):
                path.chmod(0o500)
                self.addCleanup(path.chmod, 0o700)

        def network(*_args, **_kwargs):
            raise AssertionError("healthcheck must not use the network")

        environment = {k: v for k, v in os.environ.items() if k != "MATTERMOST_TOKEN"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(socket.socket, "connect", network), \
                patch.object(socket, "create_connection", network), \
                patch.object(socket, "getaddrinfo", network):
            code, out, err = self.healthcheck()
        self.assertEqual((code, err), (0, ""), err)
        self.assertTrue(out.startswith("healthy"))
        self.assertEqual(files(self.root), before)

    def test_state_dir_resolves_like_load_config(self):
        for value in ("state", "./nested/../state", str(self.state)):
            with self.subTest(value=value):
                self.config.write_text(CONFIG.replace('"state"', json.dumps(value)), encoding="utf-8")
                with patch.dict(os.environ, {"MATTERMOST_TOKEN": "test-secret"}):
                    expected = load_config(self.config).state_dir
                self.assertEqual(cli.read_state_dir(self.config), expected)
        self.config.write_text(CONFIG.replace('state_dir = "state"\n', ""), encoding="utf-8")
        self.assertEqual(cli.read_state_dir(self.config), (self.root / "var").resolve())

    def test_command_line_exit_codes(self):
        environment = {k: v for k, v in os.environ.items() if k != "MATTERMOST_TOKEN"}
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src"), *filter(None, [environment.get("PYTHONPATH")])])

        def run(*args):
            return subprocess.run([sys.executable, "-m", "aelix_mattermost", *args], env=environment,
                                  capture_output=True, text=True, timeout=60)

        listed = run("--help")
        self.assertEqual(listed.returncode, 0)
        self.assertIn("healthcheck", listed.stdout)
        missing = run("healthcheck", "--config", str(self.config))
        self.assertEqual(missing.returncode, 1)
        self.assertTrue(missing.stderr.startswith("unhealthy: "), missing.stderr)
        self.write_health()
        healthy = run("healthcheck", "--config", str(self.config))
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        self.assertTrue(healthy.stdout.startswith("healthy"))
        self.assertEqual(run("healthcheck", "--max-age", "-1").returncode, 2)


class ResolvedModelTests(unittest.TestCase):
    def test_unresolved_models_are_configuration_errors(self):
        unresolved = [
            None, {}, "mock/mock-1", {"provider": "mock", "id": "", "api": "openai-completions"},
            {"provider": "mock", "id": "mock-1"},
            {"provider": "mock", "id": "mock-1", "api": "openai-completions", "contextWindow": True},
            # get_state of the real Aelix 0.1.0b2 without a model and with an unknown provider.
            {"provider": "", "id": "", "api": "unknown", "name": "unknown"},
            {"provider": "nonexistent", "id": "model-x", "api": "unknown", "name": "unknown"},
        ]
        for model in unresolved:
            with self.subTest(model=model), self.assertRaises(ConfigError):
                cli.resolved_model({"model": model})
        with self.assertRaisesRegex(ConfigError, "Aelix has no model"):
            cli.resolved_model({"model": unresolved[-2]})
        with self.assertRaisesRegex(ConfigError, "could not resolve the model nonexistent/model-x"):
            cli.resolved_model({"model": unresolved[-1]})

    def test_ids_unknown_to_models_json_and_the_catalog_are_rejected(self):
        # get_state of the real Aelix 0.1.0b2: an id missing from models.json under a defined
        # provider, and an id missing from a built-in provider's catalog.
        for model in ({"id": "no-such-model", "name": "no-such-model", "provider": "mock",
                       "api": "openai-completions", "maxTokens": 0, "contextWindow": 0, "input": []},
                      {"id": "no-such-claude", "name": "unknown", "provider": "anthropic",
                       "api": "anthropic-messages", "maxTokens": 0, "contextWindow": 0, "input": []}):
            with self.subTest(model=model["id"]), self.assertRaisesRegex(
                    ConfigError, f"does not know the model {model['provider']}/{model['id']}"):
                cli.resolved_model({"model": model})

    def test_resolved_model(self):
        model = {"provider": "mock", "id": "mock-1", "api": "openai-completions", "name": "Mock",
                 "contextWindow": 128000, "maxTokens": 4096}
        self.assertEqual(cli.resolved_model({"model": model}), "mock/mock-1")


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = MattermostFixture()
        await self.server.start()
        self.config = Config(url=self.server.url, token=self.server.token, allowed_users=("u1",),
                             allow_insecure_http=True, state_dir=self.root / "state",
                             work_dir=self.root / "work", command=(sys.executable, str(FAKE)),
                             rpc_timeout=5, run_timeout=5).validate()
        self.children: list[RpcProcess] = []
        self.output = io.StringIO()

    async def asyncTearDown(self):
        for child in self.children:
            await child.close()
        await self.server.close()
        self.temp.cleanup()

    async def doctor(self, check_aelix=True, **changes):
        """Run doctor; every Aelix child is recorded and must never receive a prompt."""
        children = self.children

        class Recorded(RpcProcess):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                children.append(self)

            async def run(self, text):
                raise AssertionError("doctor must never submit a prompt")

        configuration = replace(self.config, **changes).validate()
        with patch.object(cli, "RpcProcess", Recorded), contextlib.redirect_stdout(self.output):
            await cli.doctor(configuration, check_aelix)
        return self.output.getvalue()

    def assert_children_stopped(self):
        self.assertEqual(len(self.children), 1)
        self.assertFalse(self.children[0].alive)
        self.assertIsNotNone(self.children[0].process.returncode)

    async def test_reports_the_bot_websocket_and_resolved_model(self):
        output = await self.doctor()
        for line in ("Mattermost bot: @aelix (bot)",
                     "Mattermost WebSocket: authenticated (hello from server 11.11.1)",
                     "Aelix model: fake/fake-1", "Aelix tools: disabled (--no-tools)",
                     "Aelix MCP servers: disabled (ambient mcp.json files are ignored)",
                     "Aelix RPC: ready (no prompt or model request submitted)"):
            self.assertIn(line + "\n", output)
        self.assertNotIn("Warning", output)
        self.assert_children_stopped()
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(self.server.challenges, 0)  # header authentication only

    async def test_without_check_aelix_no_child_starts(self):
        output = await self.doctor(check_aelix=False)
        self.assertIn("Mattermost WebSocket: authenticated", output)
        self.assertNotIn("Aelix", output)
        self.assertEqual(self.children, [])

    async def test_unresolved_model_fails(self):
        with self.assertRaisesRegex(ConfigError, "Aelix has no model: set aelix.model"):
            await self.doctor(command=(sys.executable, str(FAKE), "--no-model"))
        self.assertNotIn("ready", self.output.getvalue())
        self.assert_children_stopped()

    async def test_model_id_aelix_does_not_know_fails(self):
        with self.assertRaisesRegex(ConfigError, "does not know the model fake/fake-typo"):
            await self.doctor(model="fake/fake-typo")
        self.assertNotIn("ready", self.output.getvalue())
        self.assert_children_stopped()

    async def test_websocket_closed_before_hello_fails_clearly(self):
        self.server.close_before_hello = 1
        with self.assertRaisesRegex(MattermostError, "WebSocket check failed: Mattermost closed the "
                                    "WebSocket before hello; proxies must pass WebSocket upgrades"):
            await self.doctor()
        self.assertEqual(self.children, [])  # Aelix is not started after a failed probe

    async def test_websocket_upgrade_rejected_or_silent_fails_clearly(self):
        self.server.upgrade_status = 502
        with self.assertRaisesRegex(MattermostError, r"WebSocket check failed: .*HTTP 502"):
            await self.doctor()
        self.server.upgrade_status, self.server.hello_delay = None, None
        with patch.object(cli, "WS_PROBE_TIMEOUT", 0.3), \
                self.assertRaisesRegex(MattermostError, "WebSocket check failed: .*hello"):
            await self.doctor()
        self.assertEqual(self.children, [])

    async def test_rejected_token_fails_before_the_websocket(self):
        self.server.auth_error = True
        with self.assertRaises(AuthenticationError):
            await self.doctor()
        self.assertEqual(self.server.connections, 0)

    async def test_account_checks(self):
        self.server.user = {**self.server.user, "roles": "system_user system_admin"}
        output = await self.doctor(check_aelix=False)
        self.assertIn("Warning: the bot has the system_admin role; a Member (system_user) bot "
                      "account is recommended\n", output)
        self.server.user = {**self.server.user, "roles": "system_user", "is_bot": False}
        with self.assertRaisesRegex(ConfigError, "does not belong to a bot account"):
            await self.doctor(check_aelix=False)
        self.assertEqual(cli.admin_roles({"roles": "system_user system_post_all"}), [])

    async def test_tools_and_mcp_are_reported(self):
        mcp = self.root / "mcp.json"
        mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
        output = await self.doctor(allowed_tools=("read",), mcp_config=mcp)
        self.assertIn("Aelix tools: enabled (read); tool policy acknowledged\n", output)
        self.assertIn(f"Aelix MCP servers: from {mcp}\n", output)
        self.assert_children_stopped()

    async def test_startup_failure_shows_redacted_stderr_only_when_enabled(self):
        command = (sys.executable, str(FAKE), "--wrong-session")
        with patch.dict(os.environ, {"FAKE_STDERR": "provider said: Bearer sk-live-abcdefgh12345678"}):
            with self.assertRaisesRegex(RpcError, r"Aelix did not start: .*log_aelix_stderr") as caught:
                await self.doctor(command=command)
            self.assertNotIn("Bearer", str(caught.exception))
            with self.assertRaisesRegex(RpcError, "Aelix stderr \\(redacted\\):\nprovider said: "
                                        "Bearer \\[redacted\\]") as caught:
                await self.doctor(command=command, log_aelix_stderr=True)
        self.assertNotIn("sk-live", str(caught.exception))
        self.assertTrue(all(not child.alive for child in self.children))

    async def test_missing_executable_fails_before_any_connection(self):
        with self.assertRaisesRegex(ConfigError, "not found on PATH"):
            await self.doctor(command=(str(self.root / "missing-aelix"),))
        self.assertEqual(self.server.connections, 0)


class SystemdUnitTests(unittest.TestCase):
    """deploy/aelix-mattermost.service keeps Aelix's HOME and agent dir writable (F6)."""

    def setUp(self):
        parser = configparser.ConfigParser(delimiters=("=",), strict=False, interpolation=None)
        parser.optionxform = str  # keys are case-sensitive
        self.lines = [x.strip() for x in UNIT.read_text(encoding="utf-8").splitlines()]
        parser.read_string("\n".join(x for x in self.lines if not x.startswith("Environment=")))
        self.service = parser["Service"]
        self.environment = dict(x.split("=", 2)[1:] for x in self.lines if x.startswith("Environment="))

    def test_home_and_agent_dir_are_writable_state_directories(self):
        state = "/var/lib/aelix-mattermost"
        self.assertEqual(self.environment["HOME"], f"{state}/home")
        self.assertEqual(self.environment["AELIX_CODING_AGENT_DIR"], f"{state}/aelix-agent")
        self.assertEqual(self.service["StateDirectory"].split(),
                         ["aelix-mattermost", "aelix-mattermost/home", "aelix-mattermost/aelix-agent"])
        self.assertEqual(self.service["StateDirectoryMode"], "0700")
        self.assertEqual(self.environment["PYTHONNOUSERSITE"], "1")  # HOME is writable now

    def test_hardening_and_graceful_stop_are_kept(self):
        for key, value in (("ProtectSystem", "strict"), ("ProtectHome", "read-only"),
                           ("NoNewPrivileges", "true"), ("PrivateTmp", "true"), ("UMask", "0077"),
                           ("ReadWritePaths", "/var/lib/aelix-mattermost"), ("KillMode", "mixed")):
            self.assertEqual(self.service[key], value)
        self.assertGreaterEqual(int(self.service["TimeoutStopSec"]), 30)

    def test_documented_doctor_commands_run_like_the_unit(self):
        # Without PrivateTmp, ProtectSystem=strict leaves no writable temporary directory, so
        # doctor --check-aelix would fail where the service itself works.
        header = "\n".join(x.lstrip("#") for x in self.lines if x.startswith("#"))
        guide = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        for source, text in (("unit header", header), ("deployment.md", guide)):
            command = re.search(r"systemd-run .*?--check-aelix", text, re.S)
            self.assertIsNotNone(command, source)
            pairs = re.findall(r"-p (\w+)=(\S+)", command[0])
            properties = {key: value for key, value in pairs if key != "Environment"}
            environment = dict(value.split("=", 1) for key, value in pairs if key == "Environment")
            for key in ("EnvironmentFile", "ProtectSystem", "ProtectHome", "PrivateTmp", "ReadWritePaths"):
                self.assertEqual(properties.get(key), self.service[key], f"{source}: {key}")
            for name in ("HOME", "AELIX_CODING_AGENT_DIR", "PYTHONNOUSERSITE"):
                self.assertEqual(environment.get(name), self.environment[name], f"{source}: {name}")


if __name__ == "__main__":
    unittest.main()
