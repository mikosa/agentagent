"""Review a live Codex approval from the terminal without restarting the service."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .ui.approvals import matching_file_changes


def format_review(approval, changes, command=None, reason=None):
    lines = [f"Approval: {approval['id']}", f"Type: {approval['method']}"]
    if reason:
        lines.append(f"Reason: {reason}")
    if changes:
        lines.append(f"Proposed file changes: {len(changes)}")
        for change in changes:
            lines.extend((f"\n{change.get('kind', {}).get('type', 'change')}: {change.get('path', 'unknown path')}",
                          change.get("diff", "No diff supplied")))
    elif command:
        lines.extend(("Proposed command:", command))
    else:
        lines.append("No file diff is stored for this approval. Review its metadata in the browser before deciding.")
    return "\n".join(lines)


def _changes(database_path, approval):
    params = json.loads(approval["params"])
    item_id = params.get("itemId")
    if not item_id:
        return []
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT type,item_id,payload FROM turn_items WHERE item_id=? ORDER BY rowid", (item_id,))
        items = [{"type": row["type"], "item_id": row["item_id"], "payload": json.loads(row["payload"])}
                 for row in rows]
    return matching_file_changes(item_id, items)


def main():
    parser = argparse.ArgumentParser(description="Review a pending Codex approval")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--id", help="approval ID when multiple requests are pending")
    parser.add_argument("--review-only", action="store_true", help="print the proposed change without deciding")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path not in ("", "/"):
        parser.error("the controller URL must be a local HTTP origin")
    with urlopen(args.url.rstrip("/") + "/api/approvals", timeout=10) as response:
        pending = json.load(response)
    matches = [item for item in pending if not args.id or item["id"] == args.id]
    if not matches:
        parser.error("no matching approval is pending on the running server")
    if len(matches) != 1:
        parser.error("multiple approvals are pending; pass --id to choose one")
    approval = matches[0]
    params = json.loads(approval["params"])
    print(format_review(approval, _changes(args.data / "controller.sqlite", approval),
                        command=params.get("command"), reason=params.get("reason")))
    if args.review_only:
        return
    choice = input("\nApprove, decline, or quit? [a/d/q] ").strip().lower()
    if choice not in ("a", "d"):
        print("No decision sent.")
        return
    decision = "accept" if choice == "a" else "decline"
    request = Request(args.url.rstrip("/") + f"/api/approvals/{approval['id']}/resolve",
                      data=json.dumps({"decision": decision}).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        print(f"Decision sent: {decision} (HTTP {response.status})")


if __name__ == "__main__":
    main()
