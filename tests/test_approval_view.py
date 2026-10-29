from app.ui.approvals import matching_file_changes


def test_approval_view_finds_proposed_file_changes_by_item_id():
    items = [
        {"type": "fileChange", "item_id": "other", "payload": {"changes": [{"path": "wrong"}]}},
        {"type": "fileChange", "item_id": "change-1", "payload": {"changes": [
            {"path": "/project/README.md", "kind": {"type": "add"}, "diff": "+hello"}]}},
    ]
    assert matching_file_changes("change-1", items) == [
        {"path": "/project/README.md", "kind": {"type": "add"}, "diff": "+hello"}]
    assert matching_file_changes("missing", items) == []
