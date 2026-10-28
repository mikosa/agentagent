from app.ui.history import HistoryPager, display_items


def test_message_deltas_are_combined_until_final_item_arrives():
    deltas = [
        {"type": "item/agentMessage/delta", "item_id": None, "created_at": "t1",
         "payload": {"itemId": "message-1", "delta": "Hello "}},
        {"type": "item/agentMessage/delta", "item_id": None, "created_at": "t2",
         "payload": {"itemId": "message-1", "delta": "world"}},
    ]
    partial = display_items(deltas)
    assert len(partial) == 1
    assert partial[0]["text"] == "Hello world"
    assert partial[0]["partial"]

    completed = {"type": "agentMessage", "item_id": "message-1", "created_at": "t3",
                 "payload": {"id": "message-1", "text": "Hello world!"}}
    final = display_items(deltas + [completed])
    assert len(final) == 1
    assert final[0]["text"] == "Hello world!"
    assert not final[0]["partial"]


def test_non_message_items_stay_inspectable():
    command = {"type": "commandExecution", "item_id": "command-1", "created_at": "t1",
               "payload": {"command": "pytest"}}
    assert display_items([command]) == [{**command, "text": None, "partial": False}]


def test_history_pager_loads_older_turns_and_newest_updates():
    pages = {
        None: {"data": [{"id": "new", "cursor": 3}, {"id": "middle", "cursor": 2}], "next_cursor": 2},
        2: {"data": [{"id": "old", "cursor": 1}], "next_cursor": None},
    }
    pager = HistoryPager(lambda before: pages[before])
    pager.refresh_latest()
    assert [row["id"] for row in pager.rows()] == ["middle", "new"]
    assert pager.has_more
    pager.load_older()
    assert [row["id"] for row in pager.rows()] == ["old", "middle", "new"]
    assert not pager.has_more
    pages[None] = {"data": [{"id": "newest", "cursor": 4}, {"id": "new", "cursor": 3}], "next_cursor": 3}
    pager.refresh_latest()
    assert [row["id"] for row in pager.rows()] == ["old", "middle", "new", "newest"]
