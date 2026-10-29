"""Present Codex approval requests with the related stored tool item."""


def matching_file_changes(item_id, items):
    for item in reversed(items):
        if item["type"] == "fileChange" and item["item_id"] == item_id:
            changes = item["payload"].get("changes", [])
            return changes if isinstance(changes, list) else []
    return []
