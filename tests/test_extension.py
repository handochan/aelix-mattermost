import os
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from aelix_mattermost.extension import setup as extension_setup
from aelix_mattermost.policy import setup as policy_setup


class API:
    def __init__(self):
        self.commands = {}
        self.hooks = {}

    def register_command(self, name, **options):
        self.commands[name] = options

    def on(self, name, handler, **options):
        self.hooks[name] = handler


@dataclass
class ToolCallResult:
    block: bool = False
    reason: str | None = None


class ExtensionTests(unittest.TestCase):
    def test_extension_only_registers_a_help_command(self):
        api = API()
        extension_setup(api)
        self.assertIn("aelix-mattermost run", api.commands["mattermost"]["handler"]("", None))

    def test_policy_checks_exact_tool_names_and_budget_and_resets_per_turn(self):
        hooks = types.ModuleType("aelix_agent_core.harness.hooks")
        hooks.ToolCallResult = ToolCallResult
        environment = {"AELIX_MATTERMOST_ALLOWED_TOOLS": '["read", "warehouse__query"]',
                       "AELIX_MATTERMOST_MAX_TOOL_CALLS": "2", "AELIX_MATTERMOST_POLICY_NONCE": "test"}
        with patch.dict(sys.modules, {"aelix_agent_core.harness.hooks": hooks}), patch.dict(os.environ, environment):
            api = API()
            policy_setup(api)
            guard = api.hooks["tool_call"]
            event = lambda name: types.SimpleNamespace(tool_name=name)
            self.assertTrue(guard(event("bash"), None).block)
            self.assertTrue(guard(event("other__read"), None).block)
            self.assertIsNone(guard(event("read"), None))
            self.assertIsNone(guard(event("warehouse__query"), None))
            self.assertTrue(guard(event("read"), None).block)
            api.hooks["before_agent_start"](None, None)
            self.assertIsNone(guard(event("read"), None))
