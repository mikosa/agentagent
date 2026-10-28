"""Line-delimited JSON-RPC transport for one Codex app-server subprocess."""

import asyncio
import json
import logging
from pathlib import Path

import jsonschema


SCHEMA_FILES = {
    "thread/start": "ThreadStart", "thread/resume": "ThreadResume",
    "thread/fork": "ThreadFork", "thread/turns/list": "ThreadTurnsList",
    "thread/items/list": "ThreadItemsList", "turn/start": "TurnStart",
    "turn/interrupt": "TurnInterrupt", "thread/name/set": "ThreadSetName",
}


class SchemaValidator:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parent.parent / "schemas" / "codex-app-server")
        self.cache = {}

    def validate_params(self, method, params):
        self._validate(method, params, "Params")

    def validate_response(self, method, response):
        self._validate(method, response, "Response")

    def _validate(self, method, payload, suffix):
        name = SCHEMA_FILES.get(method)
        if not name:
            return
        path = self.root / "v2" / f"{name}{suffix}.json"
        if path not in self.cache:
            self.cache[path] = json.loads(path.read_text())
        try:
            jsonschema.validate(payload, self.cache[path])
        except jsonschema.ValidationError as exc:
            raise ValueError(f"invalid {method} {suffix.lower()}: {exc.message}") from exc


class CodexRPC:
    def __init__(self, reader, writer, on_notification, on_request=None, validator=None):
        self.reader = reader
        self.writer = writer
        self.on_notification = on_notification
        self.on_request = on_request
        self.pending = {}
        self.counter = 0
        self.validator = validator

    async def request(self, method, params):
        if self.validator:
            self.validator.validate_params(method, params)
        self.counter += 1
        rpc_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[rpc_id] = future
        self.writer.write((json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}) + "\n").encode())
        await self.writer.drain()
        try:
            response = await future
            if self.validator:
                self.validator.validate_response(method, response)
            return response
        finally:
            self.pending.pop(rpc_id, None)

    async def notify(self, method, params=None):
        self.writer.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n").encode())
        await self.writer.drain()

    async def respond(self, rpc_id, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": rpc_id}
        message["error" if error else "result"] = error if error else result
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()

    async def initialize(self):
        response = await self.request("initialize", {"clientInfo": {"name": "agent-controller", "title": "Agent Controller", "version": "0.1.0"}})
        await self.notify("initialized")
        return response

    async def read_loop(self):
        queue = asyncio.Queue()

        async def handle_callbacks():
            while entry := await queue.get():
                try:
                    kind, message = entry
                    if kind == "request":
                        if self.on_request:
                            result = self.on_request(message["id"], message["method"], message.get("params", {}))
                            if asyncio.iscoroutine(result):
                                await result
                        else:
                            await self.respond(message["id"], error={"code": -32601, "message": "Approval must be handled in UI"})
                    else:
                        result = self.on_notification(message["method"], message.get("params", {}))
                        if asyncio.iscoroutine(result):
                            await result
                except Exception:
                    logging.getLogger(__name__).exception("Codex callback failed")
                finally:
                    queue.task_done()

        callback_task = asyncio.create_task(handle_callbacks())
        while line := await self.reader.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and "method" not in message:
                future = self.pending.get(message["id"])
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
            elif "method" in message and "id" in message:
                queue.put_nowait(("request", message))
            elif "method" in message:
                queue.put_nowait(("notification", message))
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Codex app-server disconnected"))
        queue.put_nowait(None)
        await callback_task


async def start_codex(on_notification, on_request=None):
    process = await asyncio.create_subprocess_exec(
        "codex", "app-server", "--stdio", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    rpc = CodexRPC(process.stdout, process.stdin, on_notification, on_request, SchemaValidator())
    reader_task = asyncio.create_task(rpc.read_loop())
    async def drain_stderr():
        while line := await process.stderr.readline():
            logging.getLogger(__name__).warning("Codex: %s", line.decode(errors="replace").rstrip())
    rpc.stderr_task = asyncio.create_task(drain_stderr())
    await rpc.initialize()
    return rpc, process, reader_task
