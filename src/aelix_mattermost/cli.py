"""Service entry point, connectivity checks, health check and graceful shutdown."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import tempfile
import time
import tomllib
from pathlib import Path

from . import __version__, pairing
from .config import Config, ConfigError, load_config
from .gateway import HEALTH_VERSION, Gateway
from .instance import instance_lock
from .mattermost import AuthenticationError, MattermostClient, MattermostError
from .prompt import Place, system_prompt
from .rpc import RpcError, RpcProcess
from .slash import SlashServer
from .storage import Store, write_context, write_text

HEALTH_MAX_AGE = 60.0  # seconds; the gateway rewrites health.json at least every 15 s
WS_PROBE_TIMEOUT = 10.0
# Roles that open the System Console; a gateway bot needs none of them.
ADMIN_ROLES = ("system_admin", "system_manager", "system_user_manager", "system_read_only_admin")
_VERSION = re.compile(r"\d+\.\d+\.\d+")


class Unhealthy(Exception):
    """health.json says the gateway is not running, is stuck or is disconnected."""


def _shown(value: object, limit: int = 80) -> str:
    """Server-provided text for the terminal: no control characters, bounded."""
    text = "".join(c if c.isprintable() else "?" for c in str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def admin_roles(me: dict) -> list[str]:
    roles = me.get("roles")
    return [x for x in roles.split() if x in ADMIN_ROLES] if isinstance(roles, str) else []


def resolved_model(state: dict) -> str:
    """provider/id of the model Aelix resolved; ConfigError when it has none.

    Without a model Aelix still starts and reports an empty default (api "unknown",
    empty provider and id); an unknown provider also reports api "unknown". An id that
    neither models.json nor Aelix's catalog defines gets contextWindow 0 (models.json
    entries default to 128000 and must be positive)."""
    model = state.get("model")
    fields = [model.get(x) if isinstance(model, dict) else None for x in ("provider", "id", "api")]
    provider, name, api = (x if isinstance(x, str) else "" for x in fields)
    if not provider and not name:
        raise ConfigError("Aelix has no model: set aelix.model to provider/id "
                          "(see models.json in Aelix's agent dir)")
    if not provider or not name or not api or api == "unknown":
        raise ConfigError(f"Aelix could not resolve the model {_shown(provider)}/{_shown(name)}: "
                          "check aelix.model and the provider in models.json in Aelix's agent dir")
    window = model.get("contextWindow") if isinstance(model, dict) else None
    if not _number(window) or window <= 0:
        raise ConfigError(f"Aelix does not know the model {_shown(provider)}/{_shown(name)}: check the "
                          "model id in aelix.model and in models.json in Aelix's agent dir")
    return f"{provider}/{name}"


def _agent_dir() -> str:
    """Where a child started from this environment finds models.json and auth.json."""
    configured = os.environ.get("AELIX_CODING_AGENT_DIR")
    if configured:
        return f"{Path(configured).expanduser()} (AELIX_CODING_AGENT_DIR)"
    return f"{Path.home() / '.aelix' / 'agent'} (default under HOME)"


def _stderr_hint(config: Config, rpc: RpcProcess) -> str:
    if not config.log_aelix_stderr:
        return " (set gateway.log_aelix_stderr = true to show Aelix's redacted stderr)"
    tail = rpc.stderr_tail()  # redacted by RpcProcess
    return f"\nAelix stderr (redacted):\n{tail}" if tail else ""


# Aelix's built-in tools (aelix_coding_agent.tools); extensions may register more.
BUILTIN_TOOLS = {
    "read": "read files", "ls": "list directories", "find": "find files by name",
    "grep": "search file contents", "write": "create or overwrite files",
    "edit": "edit files in place", "bash": "run shell commands",
}


def tool_sets(config: Config) -> dict[tuple[str, ...], list[str]]:
    """Each distinct allowed-tools list -> the channels with their own list that use it
    (the global list comes first and also applies to DMs and every other channel)."""
    sets: dict[tuple[str, ...], list[str]] = {config.allowed_tools: []}
    for channel_id, settings in config.channels.items():
        if settings.allowed_tools is not None:
            sets.setdefault(settings.allowed_tools, []).append(channel_id)
    return sets


def _where(config: Config, tools: tuple[str, ...], channels: list[str]) -> str:
    """"" for the global list, else " for channel(s) ..." """
    return "" if tools == config.allowed_tools else f" for channel(s) {', '.join(channels)}"


async def _start_child(config: Config, work: Path, tools: tuple[str, ...], where: str) -> dict:
    context = work / "request-context.json"
    write_context(context, {"server": config.url, "post_id": "doctor", "channel_id": "doctor",
                            "user_id": "doctor", "root_id": "doctor"})
    prompt_file = work / "system-prompt.md"
    write_text(prompt_file, system_prompt(config, "doctor", Place("D"), tools))
    rpc = RpcProcess(config, work, work, context, tools=tools, prompt_file=prompt_file)
    try:
        try:
            return await rpc.start()
        except RpcError as exc:
            raise RpcError(f"Aelix did not start{where}: {exc}{_stderr_hint(config, rpc)}") from exc
    finally:
        await rpc.close()


async def aelix_check(config: Config) -> None:
    """Start Aelix like the gateway does (same flags and environment) and read its state,
    once for every distinct tool list (an unknown tool name stops Aelix at startup).

    No prompt is submitted, so no model request is made."""
    models: set[str] = set()
    vision = False
    reports = []
    for tools, channels in tool_sets(config).items():
        where = _where(config, tools, channels)
        with tempfile.TemporaryDirectory(prefix="aelix-mattermost-doctor-") as directory:
            state = await _start_child(config, Path(directory).resolve(), tools, where)
        models.add(resolved_model(state))
        model = state.get("model")
        vision = isinstance(model, dict) and isinstance(model.get("input"), list) and "image" in model["input"]
        label = f"enabled ({', '.join(tools)}); tool policy acknowledged" if tools else "disabled (--no-tools)"
        reports.append(f"Aelix tools{where}: {label}")
    print(f"Aelix agent dir: {_agent_dir()}")
    print(f"Aelix model: {', '.join(_shown(x) for x in sorted(models))}")
    print("Aelix images: " + ("passed to the model" if vision else
                              "saved for tools only (the model reads text only)"))
    for line in reports:
        print(line)
    if config.extensions:
        print(f"Aelix extensions: {', '.join(_shown(x, 120) for x in config.extensions)}")
    if config.mcp_config is not None:
        print(f"Aelix MCP servers: from {config.mcp_config}")
    else:
        print("Aelix MCP servers: disabled (ambient mcp.json files are ignored)")
    print("Aelix RPC: ready (no prompt or model request submitted)")


async def doctor(config: Config, check_aelix: bool) -> None:
    """Check the bot token, the account and the WebSocket; with check_aelix also Aelix."""
    if shutil.which(config.command[0]) is None:
        raise ConfigError("aelix.command executable was not found on PATH")
    async with MattermostClient(config) as client:
        me = await client.me()
        if me.get("is_bot") is not True:
            raise ConfigError("The configured token does not belong to a bot account")
        print(f"Mattermost bot: @{_shown(me.get('username'))} ({_shown(me.get('id'))})")
        if roles := admin_roles(me):
            print(f"Warning: the bot has the {', '.join(roles)} role; a Member (system_user) "
                  "bot account is recommended")
        try:
            hello = await client.probe_websocket(WS_PROBE_TIMEOUT)
        except MattermostError as exc:
            hint = "" if isinstance(exc, AuthenticationError) else (
                "; proxies must pass WebSocket upgrades and the Authorization header")
            raise MattermostError(f"WebSocket check failed: {exc}{hint}", exc.status) from exc
        version = hello.get("server_version")
        match = _VERSION.match(version) if isinstance(version, str) else None
        server = match[0] if match else "of unknown version"
        print(f"Mattermost WebSocket: authenticated (hello from server {server})")
    print(f"Session scope: {config.session_scope}; while running, new messages: {config.busy_mode}; "
          f"progress: {config.progress}")
    access = "pairing on" if config.pairing else "pairing off"
    print(f"Access: {len(config.allowed_users)} allowed user(s), {len(config.admins)} admin(s), {access}"
          + ("; allow_all_users" if config.allow_all_users else ""))
    if config.channels:
        print(f"Channel settings: {len(config.channels)} channel(s)")
    if config.slash_listen:
        print(f"Slash command: /{config.slash_trigger} on {config.slash_listen} "
              "(Mattermost must reach it; see docs/slash-command.md)")
    if check_aelix:
        await aelix_check(config)


def read_state_dir(config_path: Path) -> Path:
    """gateway.state_dir resolved like load_config does, without reading the token."""
    path = config_path.expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read TOML configuration: {path}") from exc
    section = raw.get("gateway", {})
    if not isinstance(section, dict):
        raise ConfigError("gateway must be a table")
    value = section.get("state_dir", "var")
    if not isinstance(value, str) or not value:
        raise ConfigError("state_dir must be a path string")
    directory = Path(value).expanduser()
    return (directory if directory.is_absolute() else path.parent / directory).resolve()


def check_health(directory: Path, max_age: float = HEALTH_MAX_AGE) -> str:
    """Summarise state_dir/health.json; raise Unhealthy with a one-line reason.

    Healthy means written within max_age seconds with a connected WebSocket. Only that
    file is read: no network access, and it works on a read-only filesystem."""
    path = directory / "health.json"
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise Unhealthy(f"{path} does not exist: the gateway is not running "
                        "or uses another state_dir") from None
    except OSError as exc:
        raise Unhealthy(f"cannot read {path} ({type(exc).__name__}); "
                        "run as the service account") from None
    except ValueError:
        raise Unhealthy(f"{path} is not valid JSON") from None
    valid = isinstance(data, dict) and data.get("version") == HEALTH_VERSION
    if not valid or not _number(data.get("updated_at")):
        raise Unhealthy(f"{path} has an unsupported format")
    now = time.time()
    age = now - data["updated_at"]
    if age > max_age:
        raise Unhealthy(f"health.json is stale (written {age:.0f}s ago, limit {max_age:g}s): "
                        "the gateway stopped or is stuck")
    if age < -max_age:
        raise Unhealthy(f"health.json was written {-age:.0f}s in the future: check the system clock")
    if data.get("websocket_connected") is not True:
        raise Unhealthy("the Mattermost WebSocket is disconnected "
                        "(the gateway is reconnecting or stopped)")
    since = data.get("connected_since")
    connected = f" for {max(0.0, now - since):.0f}s" if _number(since) else ""
    return (f"healthy: Mattermost WebSocket connected{connected}; "
            f"health.json written {max(0.0, age):.0f}s ago")


async def serve(config: Config) -> None:
    with instance_lock(config.state_dir):
        store = Store(config.state_dir, config.dedup_days)
        try:
            async with MattermostClient(config) as client:
                me = await client.me()
                if me.get("is_bot") is not True:
                    raise ConfigError("The configured token does not belong to a bot account")
                if roles := admin_roles(me):
                    logging.warning("The bot has the %s role; a Member (system_user) bot account "
                                    "is recommended", ", ".join(roles))
                gateway = Gateway(config, client, store, me["id"], me["username"])
                stopped = asyncio.Event()
                loop = asyncio.get_running_loop()
                handlers: list[signal.Signals] = []
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, stopped.set)
                        handlers.append(sig)
                    except NotImplementedError:
                        pass
                slash = SlashServer(gateway) if config.slash_listen else None
                if slash is not None:
                    await slash.start()
                run = asyncio.create_task(gateway.run())
                stop = asyncio.create_task(stopped.wait())
                try:
                    logging.info("Gateway started for @%s", me["username"])
                    done, _ = await asyncio.wait({run, stop}, return_when=asyncio.FIRST_COMPLETED)
                    if run in done:
                        await run
                finally:
                    run.cancel()
                    stop.cancel()
                    await asyncio.gather(run, stop, return_exceptions=True)
                    if slash is not None:
                        await slash.close()
                    await gateway.close()
                    for sig in handlers:
                        loop.remove_signal_handler(sig)
        finally:
            store.close()


def _seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return seconds


async def pairing_command(config: Config, action: str, value: str | None) -> str:
    """Manage pairing in the gateway's database (safe while the gateway runs)."""
    store = Store(config.state_dir, config.dedup_days, owner=False)
    try:
        if action == "list":
            lines = ["Pending pairing requests:"]
            lines += [f"  {pairing.shown(code)}  user {user}  expires in {max(0, int((expires - time.time()) // 60))} min"
                      for code, user, _, expires in store.pending_pairings()] or ["  (none)"]
            lines.append("Users approved through pairing:")
            lines += [f"  {user}  approved {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))} by {by}"
                      for user, at, by in store.paired_users()] or ["  (none)"]
            return "\n".join(lines)
        if not value:
            raise ConfigError(f"pairing {action} needs a value")
        if action == "revoke":
            if not store.unpair(value):
                raise ConfigError("That user was not approved through pairing")
            return f"Revoked {value}"
        taken = store.take_pairing(pairing.normalize(value))
        if taken is None:
            raise ConfigError("No pending pairing request has that code (expired or already handled)")
        user, channel = taken
        if action == "deny":
            store.block_pairing(user, pairing.DENY_BLOCK)
            return f"Denied the request of {user}; no new code for {int(pairing.DENY_BLOCK // 3600)} hours"
        store.pair(user, "cli")
    finally:
        store.close()
    try:
        async with MattermostClient(config) as client:
            await client.post(channel, "", pairing.APPROVED)
        told = "; they were told in their DM"
    except (MattermostError, OSError) as exc:
        told = f"; could not tell them ({type(exc).__name__})"
    return f"Approved {user}{told}"


def tools_report(config: Config) -> str:
    from importlib.metadata import entry_points

    lines = ["Built-in Aelix tools (names for aelix.allowed_tools):"]
    lines += [f"  {name:<6} {summary}" for name, summary in BUILTIN_TOOLS.items()]
    found = sorted(entry_points(group="aelix.extensions"), key=lambda x: x.name)
    found = [x for x in found if not x.value.startswith("aelix_mattermost.")]
    lines.append("Installed Aelix extension packages in this Python (add the module to aelix.extensions;")
    lines.append("their tool names then go into allowed_tools):")
    lines += [f"  {x.name}: {x.value}" for x in found] or ["  (none)"]
    lines.append("Configured:")
    for tools, channels in tool_sets(config).items():
        where = _where(config, tools, channels).strip() or "DMs and other channels"
        lines.append(f"  {where}: {', '.join(tools) if tools else 'no tools'}")
    lines.append(f"  extensions: {', '.join(config.extensions) if config.extensions else 'none'}")
    lines.append("Run `aelix-mattermost doctor --check-aelix` to check that Aelix knows every tool name.")
    return "\n".join(lines)


COMMANDS = {
    "run": "serve Mattermost until SIGINT or SIGTERM",
    "doctor": "check the token, bot account and WebSocket (and Aelix with --check-aelix); "
              "never submits a prompt",
    "check-config": "validate the configuration without any network connection",
    "healthcheck": "exit 0 when the gateway's health.json is fresh and its WebSocket is connected "
                   "(no network access)",
    "pairing": "list, approve, deny or revoke pairing requests (works while the gateway runs)",
    "tools": "list the tool names and installed extension packages you can allow",
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aelix-mattermost", description="Aelix Mattermost bot gateway")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name, summary in COMMANDS.items():
        command = sub.add_parser(name, help=summary, description=summary)
        command.add_argument("--config", type=Path, default=Path("config.toml"))
        if name == "doctor":
            command.add_argument("--check-aelix", action="store_true",
                                 help="also start Aelix and require a resolved model")
        elif name == "healthcheck":
            command.add_argument("--max-age", type=_seconds, default=HEALTH_MAX_AGE, metavar="SECONDS",
                                 help=f"oldest acceptable health.json (default {HEALTH_MAX_AGE:g})")
        elif name == "pairing":
            command.add_argument("pairing_action", choices=("list", "approve", "deny", "revoke"))
            command.add_argument("value", nargs="?", help="a pairing code, or a user id for revoke")
    args = parser.parse_args(argv)
    if args.action == "healthcheck":
        # Reads gateway.state_dir and health.json only: no token, no writes, no network.
        try:
            print(check_health(read_state_dir(args.config), args.max_age))
        except (Unhealthy, ConfigError, OSError, RuntimeError) as exc:
            parser.exit(1, f"unhealthy: {exc}\n")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.name == "posix":
        os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.action == "check-config":
            print("Configuration is valid; no network connection was made.")
        elif args.action == "doctor":
            asyncio.run(doctor(config, args.check_aelix))
        elif args.action == "pairing":
            print(asyncio.run(pairing_command(config, args.pairing_action, args.value)))
        elif args.action == "tools":
            print(tools_report(config))
        else:
            asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    except (ConfigError, MattermostError, RpcError, RuntimeError, OSError) as exc:
        # Only our bounded summaries are shown; Aelix stderr only redacted and when
        # gateway.log_aelix_stderr asks for it.
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
