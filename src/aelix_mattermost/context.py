"""Read the gateway-bound caller identity from a custom domain tool."""

import json
import os
from pathlib import Path


def request_context() -> dict[str, str]:
    data = json.loads(Path(os.environ["AELIX_MATTERMOST_CONTEXT_FILE"]).read_text(encoding="utf-8"))
    keys = {"server", "post_id", "channel_id", "user_id", "root_id"}
    if not isinstance(data, dict) or set(data) != keys or any(not isinstance(x, str) for x in data.values()):
        raise ValueError("Invalid Mattermost request context")
    return data
