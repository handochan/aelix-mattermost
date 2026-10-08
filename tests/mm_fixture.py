"""Local Mattermost double that follows the real server's REST and WebSocket rules.

WebSocket (server/channels/api4/websocket.go and app/platform/web_conn.go, web_hub.go and
websocket_router.go, identical in the relevant parts from v9.11 to v11.11):

- An upgrade authenticated by the Authorization header is registered at once and receives
  {"event": "hello", "data": {"connection_id": ..., "server_version": ...}, "seq": 0}
  first; authentication_challenge is then ignored without a reply.
- An unauthenticated upgrade receives nothing. A valid challenge registers it (hello, then
  {"status": "OK", "seq_reply": n}); an invalid one, or the server's auth check after
  ``auth_close_delay`` (5 s on a real server), closes it without a close frame.
- Events carry increasing "seq". Reconnecting with connection_id and sequence_number (the
  next seq the client expects) resumes an inactive session without a new hello: missed
  events are replayed and events queued while disconnected follow. If the events are no
  longer buffered, or the session is unknown, a new session starts with a new hello.
- Only registered connections get replies to actions such as "ping".

REST: users/me; POST /posts (201, pending_post_id deduplicated for 30 s, a duplicate of a
create still in progress gets HTTP 500 api.post.deduplicate_create_post.pending); PUT
/posts/{id}/patch (props are replaced, not merged; unknown or deleted posts give 403);
DELETE /posts/{id} (404 once deleted). ``fail()`` injects REST failures.

Standalone, used by the Docker smoke test; serves until killed and appends every created,
patched and deleted post to the record file as one JSON object per line:

    python tests/mm_fixture.py --host H --port P --token T [--events FILE] [--record FILE]

``--events`` holds a JSON array of events (or one JSON event per line) sent after hello on
every new WebSocket session. Record lines look like {"action": "create", "id": ...,
"channel_id": ..., "root_id": ..., "message": ..., "props": ..., "pending_post_id": ...},
{"action": "patch", "id": ..., "message": ..., "props": ...} and {"action": "delete", "id": ...}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

SERVER_VERSION = "11.11.1"
DEAD_QUEUE_SIZE = 128
SEND_QUEUE_SIZE = 256
DEDUP_SECONDS = 30.0
_IDENTITY_PROPS = ("unsafe_links", "from_bot", "from_webhook", "from_oauth_app", "from_plugin")


def new_id() -> str:
    """A 26 character Mattermost id."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=26))


def _millis() -> int:
    return int(time.time() * 1000)


@dataclass(eq=False)
class Connection:
    """Server-side WebConn; it outlives its socket so that a client can resume it."""

    id: str
    user_id: str | None
    sequence: int = 0
    dead: list[dict] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)
    socket: web.WebSocketResponse | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MattermostFixture:
    def __init__(self, token: str = "test-secret", record: Path | None = None) -> None:
        self.token = token
        self.record = record
        self.user = {"id": "bot", "username": "aelix", "is_bot": True}
        self.url = ""
        # Observations: request JSON plus the post "id".
        self.posts: list[dict] = []
        self.patches: list[dict] = []
        self.deletes: list[dict] = []
        self.stored: dict[str, dict] = {}
        self.connections = 0
        self.upgrades: list[dict[str, str]] = []
        self.actions: list[dict] = []
        # Behaviour.
        self.websocket_events: list[dict] = []
        self.auth_error = False  # every token is invalid
        self.auth_close_delay = 0.2
        self.patch_delay = 0.0
        self.hello_delay: float | None = 0.0  # None: never send hello
        self.close_after_events = False
        self.close_before_hello = 0
        self.upgrade_status: int | None = None
        self.lose_events = 0  # buffered for replay but never written
        self.failures: list[dict] = []
        # 0.3.0 endpoints: threads, users, channels, files, typing and reactions.
        self.thread_posts: dict[str, list[dict]] = {}  # root id -> other people's posts
        self.users: dict[str, str] = {}  # user id -> username
        self.channels: dict[str, dict] = {}  # channel id -> {"type": ..., "display_name": ...}
        self.files: dict[str, dict] = {}  # file id -> {"name", "mime_type", "data"}
        self.uploads: list[dict] = []
        self.typing: list[dict] = []
        self.reactions: list[dict] = []
        self.hooks: list[dict] = []  # slash command follow-ups to /hooks/commands/{id}
        self._sessions: dict[str, Connection] = {}
        self._sockets: dict[web.WebSocketResponse, web.Request] = {}
        self._pending: dict[str, tuple[str, float]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._runner: web.AppRunner | None = None

    # -- lifecycle ---------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        app = web.Application()
        app.router.add_get("/api/v4/users/me", self.me)
        app.router.add_post("/api/v4/posts", self.create)
        app.router.add_put("/api/v4/posts/{id}/patch", self.patch)
        app.router.add_delete("/api/v4/posts/{id}", self.delete)
        app.router.add_get("/api/v4/websocket", self.websocket)
        app.router.add_get("/api/v4/posts/{id}/thread", self.thread)
        app.router.add_post("/api/v4/users/ids", self.user_ids)
        app.router.add_get("/api/v4/users/username/{name}", self.user_by_name)
        app.router.add_post("/api/v4/users/{id}/typing", self.typed)
        app.router.add_get("/api/v4/channels/{id}", self.channel)
        app.router.add_post("/api/v4/channels/direct", self.direct)
        app.router.add_post("/api/v4/reactions", self.react)
        app.router.add_get("/api/v4/files/{id}/info", self.file_info)
        app.router.add_get("/api/v4/files/{id}", self.file_data)
        app.router.add_post("/api/v4/files", self.upload)
        app.router.add_post("/hooks/commands/{id}", self.hook)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self.url = f"http://{host}:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]

    async def close(self) -> None:
        await self.drop_connections()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()

    # -- test controls -----------------------------------------------------------

    @property
    def challenges(self) -> int:
        return sum(x.get("action") == "authentication_challenge" for x in self.actions)

    @property
    def live(self) -> int:
        """Registered sessions that currently have a socket."""
        return sum(x.socket is not None for x in self._sessions.values())

    def fail(self, method: str, path: str, status: int = 503, *, body: object = None,
             headers: dict[str, str] | None = None, times: int = 1, after: bool = False,
             drop: bool = False, delay: float = 0.0) -> None:
        """Make the next ``times`` matching REST calls fail.

        ``after`` handles the request first, as when only the response is lost;
        ``drop`` closes the connection instead of answering."""
        self.failures.append({"method": method, "path": "/api/v4/" + path.lstrip("/"),
                              "status": status, "body": body, "headers": headers or {},
                              "times": times, "after": after, "drop": drop, "delay": delay})

    async def push_event(self, event: dict) -> None:
        """Broadcast an event to every registered session; inactive sessions queue it."""
        for connection in tuple(self._sessions.values()):
            if connection.socket is not None:
                await self._send(connection, event)
                continue
            connection.queued.append(event)
            if len(connection.queued) > SEND_QUEUE_SIZE:  # the server drops the session
                self._sessions.pop(connection.id, None)

    async def drop_connections(self) -> None:
        """Cut every socket without a close frame; sessions stay resumable."""
        for connection in self._sessions.values():
            connection.socket = None
        for socket in tuple(self._sockets):
            self._abort(socket)
        await asyncio.sleep(0)

    async def restart(self) -> None:
        """Simulate a server restart: sockets are cut and sessions forgotten."""
        await self.drop_connections()
        self._sessions.clear()

    # -- REST --------------------------------------------------------------------

    def valid_token(self, token: str | None) -> bool:
        return not self.auth_error and token == self.token

    def authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and self.valid_token(header[len("Bearer "):])

    @staticmethod
    def error(status: int, error_id: str, message: str = "") -> web.Response:
        return web.json_response({"id": error_id, "message": message, "detailed_error": "",
                                  "request_id": new_id(), "status_code": status}, status=status)

    def _failure(self, request: web.Request) -> dict | None:
        for failure in self.failures:
            if failure["method"] == request.method and request.path.startswith(failure["path"]):
                failure["times"] -= 1
                if failure["times"] <= 0:
                    self.failures.remove(failure)
                return failure
        return None

    async def _fail(self, request: web.Request, failure: dict) -> web.StreamResponse:
        if failure["delay"]:
            await asyncio.sleep(failure["delay"])
        if failure["drop"]:
            if request.transport is not None:
                request.transport.abort()
            raise asyncio.CancelledError
        body, status, headers = failure["body"], failure["status"], failure["headers"]
        if isinstance(body, str):
            return web.Response(text=body, status=status, headers=headers, content_type="text/plain")
        if body is None:
            body = {"id": "fixture.injected_failure", "message": "", "status_code": status}
        return web.json_response(body, status=status, headers=headers)

    async def _handle(self, request: web.Request, handler) -> web.StreamResponse:
        failure = self._failure(request)
        if failure is not None and not failure["after"]:
            return await self._fail(request, failure)
        response = await handler(request)
        if failure is not None:
            return await self._fail(request, failure)
        return response

    def _record(self, action: str, value: dict) -> None:
        if self.record is not None:
            with self.record.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"action": action, **value}, ensure_ascii=False) + "\n")

    @staticmethod
    def _invalid_props(props: object) -> bool:
        if not isinstance(props, dict):
            return props is not None
        return any(key in props and props[key] != "true" for key in _IDENTITY_PROPS)

    async def me(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._me)

    async def _me(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error",
                              "Invalid or expired session, please login again.")
        return web.json_response(self.user)

    async def create(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._create)

    async def _create(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        try:
            value = await request.json()
        except ValueError:
            return self.error(400, "api.context.invalid_body_param.app_error")
        if not isinstance(value, dict) or not value.get("channel_id") or self._invalid_props(value.get("props")):
            return self.error(400, "model.post.is_valid.app_error")
        pending, now = value.get("pending_post_id") or "", time.monotonic()
        if pending:
            known = self._pending.get(pending)
            if known is not None and known[1] > now:
                if not known[0]:
                    return self.error(500, "api.post.deduplicate_create_post.pending")
                return web.json_response(self.stored[known[0]], status=201)
            self._pending[pending] = ("", now + DEDUP_SECONDS)
        post_id, stamp = new_id(), _millis()
        self.stored[post_id] = {
            "id": post_id, "create_at": stamp, "update_at": stamp, "edit_at": 0, "delete_at": 0,
            "user_id": self.user["id"], "channel_id": value["channel_id"], "root_id": value.get("root_id", ""),
            "message": value.get("message", ""), "type": "", "props": dict(value.get("props") or {}),
            "pending_post_id": pending, "file_ids": list(value.get("file_ids") or []),
        }
        if pending:
            self._pending[pending] = (post_id, now + DEDUP_SECONDS)
        self.posts.append({**value, "id": post_id})
        self._record("create", {**value, "id": post_id})
        return web.json_response(self.stored[post_id], status=201)

    async def patch(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._patch)

    async def _patch(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        if self.patch_delay:
            await asyncio.sleep(self.patch_delay)
        try:
            value = await request.json()
        except ValueError:
            return self.error(400, "api.context.invalid_body_param.app_error")
        post = self.stored.get(request.match_info["id"])
        if post is None or post["delete_at"]:
            return self.error(403, "api.context.permissions.app_error")
        if not isinstance(value, dict) or self._invalid_props(value.get("props")):
            return self.error(400, "model.post.is_valid.app_error")
        if "message" in value:
            post["message"] = value["message"]
        if "props" in value:
            post["props"] = dict(value["props"] or {})  # a patch replaces all props
        post["update_at"] = post["edit_at"] = _millis()
        self.patches.append({"id": post["id"], **value})
        self._record("patch", {"id": post["id"], **value})
        return web.json_response(post)

    async def delete(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._delete)

    async def _delete(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        post = self.stored.get(request.match_info["id"])
        if post is None or post["delete_at"]:
            return self.error(404, "app.post.get.app_error")
        post["delete_at"] = _millis()
        self.deletes.append({"id": post["id"]})
        self._record("delete", {"id": post["id"]})
        return web.json_response({"status": "OK"})

    # -- 0.3.0 endpoints ---------------------------------------------------------

    def add_thread_post(self, root: str, user_id: str, message: str, create_at: int, **fields) -> dict:
        """A post by someone else in a thread (the root itself when its id is `root`)."""
        post = {"id": fields.pop("id", new_id()), "create_at": create_at, "update_at": create_at, "edit_at": 0,
                "delete_at": 0, "user_id": user_id, "channel_id": fields.pop("channel_id", "c1"),
                "root_id": "" if fields.get("is_root") else root, "message": message,
                "type": fields.pop("type", ""), "props": {}, "file_ids": fields.pop("file_ids", [])}
        fields.pop("is_root", None)
        self.thread_posts.setdefault(root, []).append(post)
        return post

    async def thread(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._thread)

    async def _thread(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        root = request.match_info["id"]
        posts = {x["id"]: x for x in self.thread_posts.get(root, [])}
        posts.update({k: v for k, v in self.stored.items() if v["root_id"] == root or k == root})
        if not posts:
            return self.error(404, "app.post.get.app_error")
        order = sorted(posts, key=lambda k: -posts[k]["create_at"])
        return web.json_response({"order": order, "posts": posts})

    async def user_ids(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        ids = await request.json()
        return web.json_response([{"id": x, "username": self.users[x]} for x in ids if x in self.users])

    async def user_by_name(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        name = request.match_info["name"]
        for user_id, username in self.users.items():
            if username == name:
                return web.json_response({"id": user_id, "username": username})
        return self.error(404, "app.user.missing_account.const")

    async def typed(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        self.typing.append({"user": request.match_info["id"], **(await request.json())})
        return web.json_response({"status": "OK"})

    async def channel(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        channel = self.channels.get(request.match_info["id"])
        if channel is None:
            return self.error(404, "app.channel.get.existing.app_error")
        return web.json_response({"id": request.match_info["id"], **channel})

    async def direct(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        users = sorted(await request.json())
        channel_id = ("dm" + "".join(users))[:26].ljust(26, "0")
        self.channels.setdefault(channel_id, {"type": "D", "display_name": ""})
        return web.json_response({"id": channel_id, "type": "D"}, status=201)

    async def react(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._react)

    async def _react(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        value = await request.json()
        self.reactions.append(value)
        return web.json_response(value, status=201)

    async def file_info(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        item = self.files.get(request.match_info["id"])
        if item is None:
            return self.error(404, "app.file_info.get.app_error")
        return web.json_response({"id": request.match_info["id"], "name": item["name"],
                                  "mime_type": item["mime_type"], "size": len(item["data"])})

    async def file_data(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._file_data)

    async def _file_data(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        item = self.files.get(request.match_info["id"])
        if item is None:
            return self.error(404, "app.file_info.get.app_error")
        return web.Response(body=item["data"], content_type="application/octet-stream")

    async def upload(self, request: web.Request) -> web.StreamResponse:
        return await self._handle(request, self._upload)

    async def _upload(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            return self.error(401, "api.context.session_expired.app_error")
        form = await request.post()
        infos = []
        for field_value in form.getall("files", []):
            file_id = new_id()
            data = field_value.file.read()
            self.files[file_id] = {"name": field_value.filename, "mime_type": field_value.content_type,
                                   "data": data}
            self.uploads.append({"id": file_id, "channel_id": form.get("channel_id"), "name": field_value.filename,
                                 "data": data})
            infos.append({"id": file_id, "name": field_value.filename, "size": len(data)})
        return web.json_response({"file_infos": infos, "client_ids": []}, status=201)

    async def hook(self, request: web.Request) -> web.StreamResponse:
        self.hooks.append({"id": request.match_info["id"], **(await request.json())})
        return web.Response(text="ok")

    # -- WebSocket ---------------------------------------------------------------

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        self.upgrades.append(dict(request.query))
        if self.upgrade_status is not None:  # e.g. a proxy in front of the server
            return self.error(self.upgrade_status, "fixture.upgrade_rejected")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._sockets[socket] = request
        connection = None
        try:
            user_id = self.user["id"] if self.authorized(request) else None
            connection = await self._attach(socket, request, user_id)
            if connection is not None:
                await self._serve(connection, socket)
        finally:
            self._sockets.pop(socket, None)
            if connection is not None and connection.socket is socket:
                connection.socket = None
        return socket

    async def _attach(self, socket: web.WebSocketResponse, request: web.Request,
                      user_id: str | None) -> Connection | None:
        """api4.connectWebSocket + PopulateWebConnConfig + Hub.Register + writePump start."""
        if user_id is not None and self.close_before_hello:
            self.close_before_hello -= 1
            self._abort(socket)
            return None
        requested = request.query.get("connection_id", "")
        if user_id is not None and requested:
            try:
                sequence = int(request.query.get("sequence_number", ""))
            except ValueError:
                sequence = None
            if sequence is None or len(requested) != 26 or not requested.isalnum():
                self._abort(socket)  # the server closes the socket after logging the error
                return None
            old = self._sessions.get(requested)
            if old is not None and old.socket is None and old.user_id == user_id:
                old.socket = socket
                await self._resume(old, sequence)
                return old
        connection = Connection(new_id(), user_id, socket=socket)
        if user_id is None:
            self._spawn(self._auth_check(connection, socket))
        else:
            await self._register(connection)
        return connection

    async def _register(self, connection: Connection) -> None:
        self._sessions[connection.id] = connection
        if self.hello_delay is None:
            return
        if self.hello_delay:
            await asyncio.sleep(self.hello_delay)
        await self._send(connection, self._hello(connection))
        for event in self.websocket_events:
            await self._send(connection, event)
        if self.close_after_events and connection.socket is not None:
            await connection.socket.close()

    async def _resume(self, connection: Connection, sequence: int) -> None:
        """WebConn.writePump on a reused connection: replay, or a new session after loss."""
        connection.sequence = sequence
        if sequence != 0:
            replay = [x for x in connection.dead if x["seq"] >= sequence]
            if replay and replay[0]["seq"] == sequence:
                for packet in replay:
                    connection.sequence += 1
                    await self._write(connection, packet)
            elif connection.dead and connection.dead[-1]["seq"] != sequence - 1:
                self._sessions.pop(connection.id, None)
                connection.id, connection.sequence, connection.dead = new_id(), 0, []
                self._sessions[connection.id] = connection
                await self._send(connection, self._hello(connection))
        queued, connection.queued = connection.queued, []
        for event in queued:
            await self._send(connection, event)

    def _hello(self, connection: Connection) -> dict:
        return {"event": "hello", "data": {"connection_id": connection.id, "server_version": SERVER_VERSION},
                "broadcast": {"omit_users": None, "user_id": connection.user_id, "channel_id": "",
                              "team_id": "", "connection_id": "", "omit_connection_id": ""}}

    async def _send(self, connection: Connection, event: dict) -> None:
        packet = {**event, "seq": connection.sequence}
        connection.sequence += 1
        connection.dead = [*connection.dead, packet][-DEAD_QUEUE_SIZE:]
        if self.lose_events:
            self.lose_events -= 1
            return
        await self._write(connection, packet)

    async def _write(self, connection: Connection, packet: dict) -> None:
        socket = connection.socket
        if socket is None or socket.closed:
            return
        try:
            async with connection.lock:
                await socket.send_json(packet)
        except (ConnectionError, RuntimeError):
            pass  # the event stays buffered for a replay

    async def _serve(self, connection: Connection, socket: web.WebSocketResponse) -> None:
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                if connection.user_id is None:
                    break
                continue
            try:
                action = json.loads(message.data)
            except ValueError:
                break
            if not isinstance(action, dict):
                break
            self.actions.append(action)
            await self._route(connection, socket, action)

    async def _route(self, connection: Connection, socket: web.WebSocketResponse, action: dict) -> None:
        """platform.WebSocketRouter.ServeWebSocket."""
        name, seq = action.get("action"), action.get("seq")
        if not name or not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            await self._reply(connection, seq, error="api.web_socket_router.bad_seq.app_error")
            return
        if name == "authentication_challenge":
            if connection.user_id is not None:
                return  # `if conn.GetSessionToken() != "" { return }`
            data = action.get("data")
            if not isinstance(data, dict) or not self.valid_token(data.get("token")):
                self._abort(socket)
                return
            connection.user_id = self.user["id"]
            await self._register(connection)
            await self._reply(connection, seq)
            return
        if name == "ping":
            await self._reply(connection, seq, {"text": "pong", "version": SERVER_VERSION,
                                                "server_time": _millis(), "node_id": ""})
            return
        await self._reply(connection, seq, error="api.web_socket_router.bad_action.app_error")

    async def _reply(self, connection: Connection, seq: object, data: dict | None = None,
                     error: str | None = None) -> None:
        if connection.user_id is None or connection.socket is None:
            return  # replies to unregistered connections are dropped by the hub
        packet: dict = {"status": "FAIL" if error else "OK", "seq_reply": seq}
        if data is not None:
            packet["data"] = data
        if error:
            packet["error"] = {"id": error, "message": "", "status_code": 400}
        try:
            async with connection.lock:
                await connection.socket.send_json(packet)
        except (ConnectionError, RuntimeError, AttributeError):
            pass

    async def _auth_check(self, connection: Connection, socket: web.WebSocketResponse) -> None:
        await asyncio.sleep(self.auth_close_delay)
        if connection.user_id is None:
            self._abort(socket)

    def _abort(self, socket: web.WebSocketResponse) -> None:
        request = self._sockets.get(socket)
        if request is not None and request.transport is not None:
            request.transport.abort()

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def load_events(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return value if isinstance(value, list) else [value]


async def _serve_forever(args: argparse.Namespace) -> None:
    fixture = MattermostFixture(args.token, args.record)
    if args.events is not None:
        fixture.websocket_events = load_events(args.events)
    await fixture.start(args.host, args.port)
    print(f"Mattermost fixture listening on {fixture.url}", flush=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await stopped.wait()
    finally:
        await fixture.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local Mattermost double until killed")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8065)
    parser.add_argument("--token", required=True)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--record", type=Path)
    asyncio.run(_serve_forever(parser.parse_args()))


if __name__ == "__main__":
    main()
