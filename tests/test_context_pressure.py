import asyncio

from app.controller import Controller


def test_smaller_model_window_triggers_early_split_review(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", object())
        app.db.conn.execute("INSERT INTO sessions (id,thread_id,title,objective,cwd,created_at) VALUES ('s','t','A','Goal',?,datetime('now'))", (str(tmp_path),))
        await app.handle_notification("thread/tokenUsage/updated", {"threadId": "t", "turnId": "x", "tokenUsage": {"last": {"totalTokens": 110000}, "modelContextWindow": 128000}})
        session = app.session("s")
        assert session["context_window"] == 128000
        assert app.should_evaluate_split(session)

    asyncio.run(scenario())
