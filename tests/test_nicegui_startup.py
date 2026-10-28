import asyncio
from types import SimpleNamespace

from app.nicegui_main import graph_clicked_session_id, refresh_chart, stop_codex_process, wait_for_controller


def test_page_waits_for_controller_startup():
    async def scenario():
        runtime = {"ready": asyncio.Event()}

        async def initialize():
            await asyncio.sleep(0)
            runtime["controller"] = object()
            runtime["ready"].set()

        task = asyncio.create_task(initialize())
        assert await wait_for_controller(runtime, timeout=1) is runtime["controller"]
        await task

    asyncio.run(scenario())


def test_chart_refresh_mutates_read_only_options():
    class Chart:
        def __init__(self):
            self._options = {"old": True}
            self.updated = False

        @property
        def options(self):
            return self._options

        def update(self):
            self.updated = True

    chart = Chart()
    refresh_chart(chart, {"series": [{"data": [1]}]})
    assert chart.options == {"series": [{"data": [1]}]}
    assert chart.updated


def test_shutdown_tolerates_already_exited_codex_process():
    class ExitedProcess:
        returncode = 1

        def terminate(self):
            raise ProcessLookupError()

        async def wait(self):
            return self.returncode

    asyncio.run(stop_codex_process(ExitedProcess()))


def test_echart_node_click_uses_nicegui_event_data():
    assert graph_clicked_session_id(SimpleNamespace(data={"id": "session-1"})) == "session-1"
    assert graph_clicked_session_id(SimpleNamespace(data={"source": "a", "target": "b"})) is None


def test_startup_error_is_reported_instead_of_missing_controller():
    async def scenario():
        runtime = {"ready": asyncio.Event(), "error": "Codex could not initialize"}
        runtime["ready"].set()
        try:
            await wait_for_controller(runtime, timeout=1)
        except RuntimeError as exc:
            assert "Codex could not initialize" in str(exc)
        else:
            assert False, "startup error should surface"

    asyncio.run(scenario())
