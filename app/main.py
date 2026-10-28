"""Run the loopback browser service and controller scheduler."""

import argparse
import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .api import LocalAPI
from .codex_rpc import start_codex
from .controller import Controller


LOG = logging.getLogger(__name__)
INDEX = Path(__file__).parent / "static" / "index.html"


def trusted_origin(origin, port):
    if origin is None:
        return True  # Local MCP/CLI clients do not send Origin.
    parsed = urlsplit(origin)
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == port


def serve_http(api, loop, host, port):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            if self.path == "/" and self.command == "GET":
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
                return
            if not self.path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            if self.command != "GET":
                if not trusted_origin(self.headers.get("Origin"), self.server.server_port):
                    self._send(403, {"error": "untrusted browser origin"})
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    self._send(415, {"error": "JSON content type required"})
                    return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 1024 * 1024:
                    self._send(413, {"error": "request too large"})
                    return
                body = json.loads(self.rfile.read(size)) if size else {}
                future = asyncio.run_coroutine_threadsafe(api.execute(self.command, self.path, body), loop)
                status, result = future.result(timeout=60)
                self._send(status, result)
            except (ValueError, TimeoutError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                LOG.exception("HTTP request failed")
                self._send(500, {"error": "internal error"})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle

        def log_message(self, fmt, *args):
            LOG.info("HTTP %s", fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return server


async def scheduler(controller):
    tick = 0
    while True:
        try:
            await controller.dispatch_once()
            await controller.dispatch_reflection_once()
            if tick % 40 == 0:
                await controller.fire_due_cron()
        except Exception:
            LOG.exception("scheduler tick failed")
        tick += 1
        await asyncio.sleep(0.25)


async def run(args):
    loop = asyncio.get_running_loop()
    rpc, process, reader_task = await start_codex(lambda _m, _p: None)
    controller = Controller(args.data / "controller.sqlite", rpc, mcp_url=f"http://{args.host}:{args.port}")
    rpc.on_notification = controller.handle_notification
    rpc.on_request = controller.record_approval
    controller.recover_inflight()
    await controller.reconcile_inflight()
    await controller.resume_pending_splits()
    api = LocalAPI(controller)
    server = serve_http(api, loop, args.host, args.port)
    task = asyncio.create_task(scheduler(controller))
    LOG.info("Agent Controller: http://%s:%s", args.host, server.server_port)
    try:
        await reader_task
    finally:
        task.cancel()
        server.shutdown()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def main():
    parser = argparse.ArgumentParser(description="Local Codex agent controller")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("remote binding requires authentication; use a loopback host")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
