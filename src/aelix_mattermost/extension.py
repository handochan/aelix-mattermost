"""The optional host extension never starts a gateway during import/reload."""

from typing import Any


def setup(aelix: Any) -> None:
    def mattermost(_args: str, _context: Any) -> str:
        return (
            "Mattermost gateway: run `aelix-mattermost doctor --config config.toml`, "
            "then `aelix-mattermost run --config config.toml` in a separate service. "
            "In Mattermost use @aelix, !help, !cancel or !reset."
        )

    aelix.register_command(
        "mattermost", handler=mattermost,
        description="Show how to run the independent Mattermost gateway.",
    )
