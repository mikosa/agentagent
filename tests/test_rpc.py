import asyncio
import json

from app.codex_rpc import CodexRPC


class Writer:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(json.loads(data))

    async def drain(self):
        pass


def test_rpc_correlates_responses_and_delivers_notifications():
    async def scenario():
        reader = asyncio.StreamReader()
        writer = Writer()
        seen = []
        rpc = CodexRPC(reader, writer, lambda method, params: seen.append((method, params)))
        task = asyncio.create_task(rpc.read_loop())
        request = asyncio.create_task(rpc.request("thread/read", {"threadId": "x"}))
        await asyncio.sleep(0)
        rpc_id = writer.lines[0]["id"]
        reader.feed_data((json.dumps({"method": "turn/completed", "params": {"threadId": "x"}}) + "\n").encode())
        reader.feed_data((json.dumps({"id": rpc_id, "result": {"ok": True}}) + "\n").encode())
        assert await request == {"ok": True}
        await asyncio.sleep(0)
        assert seen == [("turn/completed", {"threadId": "x"})]
        reader.feed_eof()
        await task

    asyncio.run(scenario())


def test_notification_handler_can_issue_followup_rpc():
    async def scenario():
        reader = asyncio.StreamReader()
        writer = Writer()
        seen = []
        rpc = None

        async def handler(method, params):
            result = await rpc.request("thread/read", {"threadId": "x"})
            seen.append(result)

        rpc = CodexRPC(reader, writer, handler)
        task = asyncio.create_task(rpc.read_loop())
        reader.feed_data(b'{"method":"turn/completed","params":{}}\n')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert writer.lines
        reader.feed_data((json.dumps({"id": writer.lines[0]["id"], "result": {"ok": True}}) + "\n").encode())
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        assert seen == [{"ok": True}]

    asyncio.run(scenario())
