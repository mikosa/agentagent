import sqlite3

from app.db import Database


def test_backup_captures_committed_wal_state_and_refuses_overwrite(tmp_path):
    db = Database(tmp_path / "controller.sqlite")
    db.event("test.saved", "test", "session-1", {"value": 42})
    output = tmp_path / "snapshot.sqlite"
    db.backup(output)
    with sqlite3.connect(output) as copy:
        assert copy.execute("SELECT type FROM events").fetchone()[0] == "test.saved"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    try:
        db.backup(output)
    except FileExistsError:
        pass
    else:
        assert False, "backup should not overwrite an earlier snapshot"
