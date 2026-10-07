"""Service entry point, connectivity checks and graceful shutdown."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import tempfile
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .gateway import Gateway
from .instance import instance_lock
from .mattermost import MattermostClient, MattermostError
from .rpc import RpcError, RpcProcess
from .storage import Store, write_context


async def doctor(config: Config, check_aelix: bool) -> None:
    executable = config.command[0]
    if shutil.which(executable) is None:
        raise ConfigError("aelix.command executable was not found on PATH")
    async with MattermostClient(config) as client:
        me = await client.me()
        if me.get("is_bot") is not True:
            raise ConfigError("MATTERMOST_TOKEN must belong to a bot account")
        print(f"Mattermost bot: @{me['username']} ({me['id']})")
        print(f"Session scope: {config.session_scope}; allowed tools: {len(config.allowed_tools)}")
    if check_aelix:
        with tempfile.TemporaryDirectory(prefix="aelix-mattermost-doctor-") as directory:
            work = Path(directory)
            context = work / "request-context.json"
            write_context(context, {"server": config.url, "post_id": "doctor", "channel_id": "doctor",
                                    "user_id": "doctor", "root_id": "doctor"})
            rpc = RpcProcess(config, work, work, context)
            try:
                await rpc.start()
                print("Aelix RPC: ready (no prompt or model request submitted)")
            finally:
                await rpc.close()


async def serve(config: Config) -> None:
    with instance_lock(config.state_dir):
        store = Store(config.state_dir, config.dedup_days)
        try:
            async with MattermostClient(config) as client:
                me = await client.me()
                if me.get("is_bot") is not True:
                    raise ConfigError("The configured token does not belong to a bot account")
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
                    await gateway.close()
                    for sig in handlers:
                        loop.remove_signal_handler(sig)
        finally:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Aelix Mattermost bot gateway")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "doctor", "check-config"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.toml"))
        if name == "doctor":
            command.add_argument("--check-aelix", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.name == "posix":
        os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.action == "check-config":
            print("Configuration is valid; no network connection was made.")
        elif args.action == "doctor":
            asyncio.run(doctor(config, args.check_aelix))
        else:
            asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    except (ConfigError, MattermostError, RpcError, RuntimeError, OSError) as exc:
        # Only our bounded exception summaries are displayed, never raw provider stderr.
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
