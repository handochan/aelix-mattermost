"""A deterministic stand-in for `aelix --mode rpc` that follows the real wire.

Responses are shaped like rpc_types (``type`` first). Events are shaped like
``dataclasses.asdict`` of the harness events: snake_case keys, ``type`` last.
Commands are dispatched concurrently and ``prompt`` is preflighted against the
harness phase, so a prompt sent while a run, retry backoff or compaction is in
progress is rejected "busy" exactly like Aelix does.

Prompt markers: ``__error__`` (non-retryable provider error), ``__retry__``
(error, auto_retry_start, a second run, auto_retry_end), ``__overflow__``
(context overflow, overflow compaction, a second run), ``__compact__`` (answer,
then a threshold-compaction busy window after agent_end), ``__huge__`` (a 3 MiB
tool result inside the run and agent_end), ``__stream__`` (message_update
events, one of them deliberately unparsable), ``__tool__`` (a tool process in
its own session, like Aelix's bash tool: abort kills it, killpg does not),
``__malformed__`` (a stray non-JSON stdout line mid-run), ``__hang__`` (never
finishes until aborted), ``__holder__`` (hangs while a descendant in another
session holds stdout/stderr open), ``__nostart__`` (accepted but no run, like
an input hook that handled it), ``__slow__``, ``__close_stdout__`` (stdout ends,
the process lives on) and ``__exit__`` (the child dies). ``__compact__`` compacts for
0.6 s, or until the file named by FAKE_COMPACTION_UNTIL exists.

Like Aelix, the process owns its session file through a ``<file>.lock`` flock
for its whole life and refuses to start on a file another live process owns.
``--model provider/id`` other than fake/fake-1 resolves like an id that neither
models.json nor the catalog defines: contextWindow 0.

SIGTERM exits at once without stopping tools; stdin EOF aborts like dispose().
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(allow_abbrev=False)
parser.add_argument("--session-dir", required=True)
parser.add_argument("--session")
parser.add_argument("--tools")
parser.add_argument("--missing-policy", action="store_true")
parser.add_argument("--wrong-session", action="store_true")
parser.add_argument("--noisy", action="store_true", help="print a non-JSON stdout line at startup")
parser.add_argument("--stderr-flood", action="store_true", help="a 2 MiB stderr line before the policy line")
parser.add_argument("--no-model", action="store_true", help="report Aelix's unresolved default model")
parser.add_argument("--slow-start", type=float, default=0, help="seconds of cold start before serving")
parser.add_argument("--model")
args, _ = parser.parse_known_args()
directory = Path(args.session_dir)
directory.mkdir(parents=True, exist_ok=True)
filename = Path(args.session) if args.session else directory / (uuid.uuid4().hex + ".jsonl")
if args.wrong_session:
    filename = directory.parent / "escaped.jsonl"
count = len(filename.read_text().splitlines()) if filename.exists() else 0
SESSION_ID = uuid.uuid4().hex
BIG = 3 * 1024 * 1024
# Without a configured model Aelix reports its empty default Model, not null.
MODEL = {"id": "", "name": "unknown", "provider": "", "api": "unknown", "maxTokens": 0,
         "contextWindow": 0, "input": []} if args.no_model else {
    "id": "fake-1", "name": "Fake", "provider": "fake", "api": "fake-api", "maxTokens": 4096,
    "contextWindow": 128000, "input": ["text"]}
if args.model and args.model != "fake/fake-1":  # an id Aelix does not know: its provider's api
    provider, _, model_id = args.model.partition("/")
    MODEL = {"id": model_id, "name": model_id, "provider": provider,
             "api": "fake-api" if provider == "fake" else "unknown", "maxTokens": 0,
             "contextWindow": 0, "input": []}
MODEL.update({"cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
              "thinkingLevelMap": None, "reasoning": False, "baseUrl": ""})


def own_session(path):
    """SessionWriterLock: a kernel flock on <session>.lock, held until the process exits.
    RPC mode cannot ask the user, so a file still owned after 0.25 s is refused."""
    try:
        import fcntl
    except ImportError:  # Windows: Aelix uses LockFileEx; not emulated here
        return None
    handle = open(str(path) + ".lock", "a")
    deadline = time.monotonic() + 0.25
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() > deadline:
                print(f"Error: this session is already open in another process: {path}",
                      file=sys.stderr, flush=True)
                sys.exit(1)
            time.sleep(0.02)


OWNER = own_session(filename)
state = {"phase": "idle", "open": False, "task": None, "last": "old answer that must not leak into a failed turn"}
conversation: list[dict] = []


def write(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit(kind, **fields):
    write({**fields, "type": kind})


def respond(packet, data=None, error=None, command=None):
    out = {"type": "response", "command": command or packet.get("type"), "success": error is None}
    if error is not None:
        out["error"] = error
    if isinstance(packet.get("id"), str):
        out["id"] = packet["id"]
    if data is not None:
        out["data"] = data
    write(out)


def text_part(text):
    return {"text": text, "text_signature": "", "type": "text"}


def user(text):
    return {"content": [text_part(text)], "timestamp": None, "role": "user"}


def assistant(text="", reason="stop", error=None, content=None):
    return {"content": content if content is not None else ([text_part(text)] if text else []),
            "stop_reason": reason, "error_message": error, "usage": None, "timestamp": None,
            "api": "fake-api", "provider": "fake", "model": "fake-1", "response_id": None,
            "role": "assistant"}


def tool_call(call_id, name, arguments):
    return assistant(reason="toolUse", content=[{
        "tool_call_id": call_id, "tool_name": name, "input": arguments,
        "thought_signature": "", "type": "toolCall"}])


def tool_result(call_id, name, text):
    return {"tool_call_id": call_id, "content": [text_part(text)], "is_error": False,
            "timestamp": None, "tool_name": name, "role": "toolResult"}


def message(value, start=True):
    if start:
        emit("message_start", message=value)
    emit("message_end", message=value)


async def run(text, final, middle=None, updates=1, noise=None):
    """One agent_start..agent_end run of the agent loop; `noise` is a raw stdout line."""
    new = []
    state["open"] = True
    emit("agent_start")
    emit("turn_start")
    if text is not None:
        new.append(user(text))
        message(new[-1])
    if middle is not None:
        new.extend(await middle())
        emit("turn_start")
    if final["stop_reason"] == "error":
        message(final, start=False)  # a failed request never streamed a partial
    else:
        emit("message_start", message=assistant())
        for size in range(1, updates + 1):  # each update carries the whole partial message
            emit("message_update", message=assistant("x" * size),
                 assistant_message_event={"delta": "x", "type": "text_delta"})
        if noise is not None:
            print(noise, flush=True)
        message(final, start=False)
    emit("turn_end", message=final, tool_results=[])
    new.append(final)
    conversation.extend(new)
    state["open"] = False
    emit("agent_end", messages=new)


async def tool_turn(call, result, work=None):
    message(call)
    emit("tool_execution_start", tool_call_id=call["content"][0]["tool_call_id"],
         tool_name=result["tool_name"], args=call["content"][0]["input"])
    if work is not None:
        await work()
    emit("tool_execution_end", tool_call_id=result["tool_call_id"],
         result={"content": result["content"], "details": None}, tool_name=result["tool_name"],
         is_error=False)
    message(result)
    emit("turn_end", message=call, tool_results=[result])
    return [call, result]


async def sleeping_tool():
    # Aelix's bash tool runs commands in their own session: killpg on the
    # Aelix process group does not reach them, only an abort does.
    process = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    (directory / "tool.pid").write_text(str(process.pid))
    try:
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def compaction(reason, seconds):
    emit("compaction_start", reason=reason)
    state["phase"] = "compaction"
    until = os.environ.get("FAKE_COMPACTION_UNTIL") if reason == "threshold" else None
    if until:
        while not Path(until).exists():
            await asyncio.sleep(0.02)
    else:
        await asyncio.sleep(seconds)
    state["phase"] = "turn"
    emit("compaction_end", reason=reason, result={"summary": "fake summary", "first_kept_entry_id": "x",
         "tokens_before": 1, "details": None}, aborted=False, will_retry=reason == "overflow",
         error_message=None)


async def scenario(text, reply):
    secret = "provider-secret-must-not-be-posted"
    if text == "__error__":
        await run(text, assistant(reason="error", error=secret))
    elif text.startswith("__retry__"):
        await run(text, assistant(reason="error", error="503 overloaded " + secret))
        emit("auto_retry_start", attempt=1, max_attempts=3, delay_ms=100, error_message="503 overloaded")
        await asyncio.sleep(0.1)
        await run(None, assistant(reply))
        emit("auto_retry_end", success=True, attempt=1, final_error=None)
    elif text.startswith("__overflow__"):
        await run(text, assistant(reason="error", error="maximum context length is 20000 tokens"))
        await compaction("overflow", 0.3)
        await run(None, assistant(reply))
    elif text.startswith("__compact__"):
        await run(text, assistant(reply))
        await compaction("threshold", 0.6)
    elif text.startswith("__huge__"):
        big = tool_result("call_big", "mm_big", "B" * BIG)
        await run(text, assistant(reply), lambda: tool_turn(tool_call("call_big", "mm_big", {}), big))
    elif text.startswith("__tool__"):
        result = tool_result("call_sleep", "bash", "woke")
        await run(text, assistant(reply), lambda: tool_turn(
            tool_call("call_sleep", "bash", {"command": "sleep 30"}), result, sleeping_tool))
    elif text.startswith("__stream__"):
        # The last update is unparsable on purpose: a client must not JSON-parse them.
        await run(text, assistant(reply), updates=300,
                  noise='{"message": {"content": [truncated, "type": "message_update"}')
    elif text in ("__hang__", "__holder__"):
        if text == "__holder__":  # inherits our stdout/stderr, outside our process group
            holder = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
            (directory / "holder.pid").write_text(str(holder.pid))
        state["open"] = True
        emit("agent_start")
        message(user(text))
        await asyncio.Event().wait()
    else:
        if text.startswith("__slow__"):
            await asyncio.sleep(0.08)
        await run(text, assistant(reply), noise="not JSON" if text == "__malformed__" else None)


async def prompt(text):
    """AgentHarness.prompt: hold the phase through every run and the tail."""
    global count
    state["phase"] = "turn"
    try:
        if text == "__nostart__":
            return
        if text == "__exit__":
            os._exit(7)
        if text == "__close_stdout__":
            os.close(1)
            await asyncio.Event().wait()
        count += 1
        with filename.open("a") as handle:
            handle.write(json.dumps({"prompt": text}) + "\n")
        reply = f"turn {count}: {text}"
        await scenario(text, reply)
        if text != "__error__":
            state["last"] = reply
    except asyncio.CancelledError:
        if state["open"]:  # the abort close-out of Aelix's _run
            aborted = assistant(reason="aborted")
            emit("turn_end", message=aborted, tool_results=[])
            emit("agent_end", messages=[*conversation, aborted])
            state["open"] = False
    finally:
        state["phase"] = "idle"


async def abort():
    task = state["task"]
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def session_state():
    return {"model": MODEL, "thinkingLevel": "off", "isStreaming": state["phase"] != "idle",
            "isCompacting": state["phase"] == "compaction", "steeringMode": "one-at-a-time",
            "followUpMode": "one-at-a-time", "sessionFile": str(filename), "sessionId": SESSION_ID,
            "sessionName": None, "autoCompactionEnabled": True, "autoRetryEnabled": True,
            "messageCount": len(conversation), "pendingMessageCount": 0,
            # Test-only probes of what the gateway let the child inherit.
            "tokenVisible": "MATTERMOST_TOKEN" in os.environ,
            "probe": {"mcpConfig": os.environ.get("AELIX_MCP_CONFIG"),
                      "subagentDepth": os.environ.get("AELIX_SUBAGENT_DEPTH"), "argv": sys.argv[1:]}}


async def dispatch(packet):
    command = packet.get("type")
    if command == "prompt":
        if state["phase"] != "idle":
            respond(packet, error=f"AgentHarness is busy (phase={state['phase']!r}); send "
                    '"streamingBehavior": "steer" | "followUp" to enqueue, or use the steer / '
                    "follow_up commands.")
            return
        state["task"] = asyncio.create_task(prompt(packet["message"]))
        respond(packet)
    elif command == "get_state":
        respond(packet, session_state())
    elif command == "get_last_assistant_text":
        respond(packet, {"text": state["last"]})
    elif command == "get_messages":
        respond(packet, {"messages": conversation})
    elif command == "abort":
        await abort()
        respond(packet)
    else:
        respond(packet, error=f"Failed to parse command: Unknown command type: {command}", command="parse")


async def main():
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: os._exit(0))
    reader = asyncio.StreamReader(limit=1 << 30)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks = set()
    while line := await reader.readline():
        if line.strip():
            task = asyncio.create_task(dispatch(json.loads(line)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    await abort()


if args.slow_start:
    time.sleep(args.slow_start)
if args.noisy:
    print("domain extension loaded: connecting to backend...", flush=True)
if os.environ.get("FAKE_STDERR"):
    print(os.environ["FAKE_STDERR"], file=sys.stderr, flush=True)
if args.stderr_flood:
    sys.stderr.write("x" * (2 * 1024 * 1024) + "\n")
    sys.stderr.flush()
if args.tools and not args.missing_policy:
    print("aelix-mattermost-policy-ready:" + os.environ["AELIX_MATTERMOST_POLICY_NONCE"],
          file=sys.stderr, flush=True)
asyncio.run(main())
