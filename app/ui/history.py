"""Readable chat items built from durable Codex notifications."""

import re


def work_source_label(source):
    """Name the origin of a turn's submitted prompt."""
    return {
        "manual": ("You", "user"),
        "agent": ("Another agent", "agent"),
        "cron": ("Schedule", "activity"),
    }.get(source, (str(source or "Prompt").replace("_", " ").title(), "activity"))


def chat_item_presentation(item, commands=None):
    """Return a readable source, visual category, and short detail for an item."""
    kind = item["type"]
    payload = item["payload"]
    if kind == "agentMessage":
        return "Agent", "agent", ""
    if kind == "userMessage":
        return "You", "user", ""
    if kind == "commandExecution":
        command = payload.get("command") or ""
        script_path = payload.get("scriptPath") or ""
        is_python = script_path.endswith(".py") or bool(re.search(r"\bpython(?:3(?:\.\d+)?)?\b", command))
        return ("Python script" if is_python else "Tool"), "tool", command.splitlines()[0][:120]
    if kind == "item/commandExecution/outputDelta":
        command = (commands or {}).get(payload.get("itemId"))
        if command and chat_item_presentation({"type": "commandExecution", "payload": command})[0] == "Python script":
            return "Python output", "tool", ""
        return "Tool output", "tool", ""
    if kind == "webSearch":
        return "Web search", "tool", payload.get("query") or ""
    if kind == "fileChange":
        return "File change", "tool", ""
    return "Agent activity", "activity", kind


def user_message_text(payload):
    content = payload.get("content") or []
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return payload.get("text") or ""


def conversation_revision(db, session_id):
    """Return the last journal entry affecting a session's conversation."""
    row = db.one("SELECT COALESCE(MAX(rowid), 0) AS revision FROM events WHERE entity_id=?", (session_id,))
    return row["revision"]


class HistoryPager:
    """Keep loaded local turns while fetching newer and older pages."""

    def __init__(self, load_page):
        self.load_page = load_page
        self.by_id = {}
        self.next_cursor = None
        self.initialized = False

    @property
    def has_more(self):
        return self.next_cursor is not None

    def refresh_latest(self):
        page = self.load_page(None)
        newest_known = max((row["cursor"] for row in self.by_id.values()), default=None)
        for row in page["data"]:
            self.by_id[row["id"]] = row
        if not self.initialized:
            self.next_cursor = page["next_cursor"]
            self.initialized = True
        elif newest_known is not None and page["data"] and min(row["cursor"] for row in page["data"]) > newest_known:
            self.next_cursor = page["next_cursor"]

    def load_older(self):
        if self.next_cursor is None:
            return
        page = self.load_page(self.next_cursor)
        for row in page["data"]:
            self.by_id[row["id"]] = row
        self.next_cursor = page["next_cursor"]

    def rows(self):
        return sorted(self.by_id.values(), key=lambda row: row["cursor"])


def display_items(items):
    """Show one partial message per item ID until Codex supplies its final text."""
    completed = {item["payload"].get("id") or item.get("item_id") for item in items
                 if item["type"] == "agentMessage"}
    result = []
    partial_by_id = {}
    for item in items:
        payload = item["payload"]
        if item["type"] == "item/agentMessage/delta":
            message_id = payload.get("itemId")
            if not message_id or message_id in completed:
                continue
            if message_id not in partial_by_id:
                partial = {**item, "type": "agentMessage", "text": "", "partial": True}
                partial_by_id[message_id] = partial
                result.append(partial)
            partial_by_id[message_id]["text"] += payload.get("delta", "")
        elif item["type"] == "agentMessage":
            result.append({**item, "text": payload.get("text", ""), "partial": False})
        elif item["type"] == "userMessage":
            result.append({**item, "text": user_message_text(payload), "partial": False})
        else:
            result.append({**item, "text": None, "partial": False})
    return result
