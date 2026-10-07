"""Loaded explicitly by each tool-enabled Aelix child, before its first prompt."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def tool_allowed(name: str, names: frozenset[str]) -> bool:
    return name in names


def setup(aelix: Any) -> None:
    from aelix_agent_core.harness.hooks import ToolCallResult

    raw = json.loads(os.environ["AELIX_MATTERMOST_ALLOWED_TOOLS"])
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x for x in raw):
        raise ValueError("Invalid Mattermost tool policy")
    names = frozenset(raw)
    maximum = int(os.environ["AELIX_MATTERMOST_MAX_TOOL_CALLS"])
    if maximum <= 0:
        raise ValueError("Invalid tool-call budget")
    calls = 0

    def reset(_event: Any, _context: Any) -> None:
        nonlocal calls
        calls = 0

    def guard(event: Any, _context: Any) -> Any:
        nonlocal calls
        if not tool_allowed(event.tool_name, names):
            return ToolCallResult(block=True, reason="This tool is not allowed by the Mattermost gateway.")
        if calls >= maximum:
            return ToolCallResult(block=True, reason="The Mattermost tool-call budget is exhausted.")
        calls += 1
        return None

    aelix.on("before_agent_start", reset, error_mode="throw")
    aelix.on("tool_call", guard, error_mode="throw")
    nonce = os.environ["AELIX_MATTERMOST_POLICY_NONCE"]
    print(f"aelix-mattermost-policy-ready:{nonce}", file=sys.stderr, flush=True)
