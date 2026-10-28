"""Readable chat items built from durable Codex notifications."""


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
        else:
            result.append({**item, "text": None, "partial": False})
    return result
