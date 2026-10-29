"""NiceGUI/ECharts entry point for the controller browser interface.

Install the optional ``ui`` extra before using this entry point.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .api import LocalAPI
from .codex_rpc import start_codex
from .controller import Controller
from .main import scheduler, trusted_origin
from .ui.graph import graph_options
from .ui.approvals import matching_file_changes
from .ui.history import HistoryPager, conversation_revision, display_items


SIMPLE_APPROVAL_METHODS = {
    "item/permissions/requestApproval", "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval", "execCommandApproval", "applyPatchApproval",
}


async def wait_for_controller(runtime, timeout=30):
    """Wait for NiceGUI's asynchronous startup callback before serving a page."""
    try:
        await asyncio.wait_for(runtime["ready"].wait(), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Controller startup is still in progress; reload shortly") from exc
    if error := runtime.get("error"):
        raise RuntimeError(f"Controller startup failed: {error}")
    return runtime["controller"]


def refresh_chart(chart, options):
    chart.options.clear()
    chart.options.update(options)
    chart.update()


def graph_clicked_session_id(event):
    data = getattr(event, "data", None)
    return data.get("id") if isinstance(data, dict) else None


async def stop_codex_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def main():
    try:
        from nicegui import app, ui
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:
        raise SystemExit("NiceGUI is required: python3 -m pip install -e '.[ui]'") from exc

    parser = argparse.ArgumentParser(description="Agent Controller with NiceGUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost") or not 1 <= args.port <= 65535:
        parser.error("use a loopback host and port 1–65535")

    runtime = {"ready": asyncio.Event()}

    async def startup():
        try:
            rpc, process, reader_task = await start_codex(lambda _m, _p: None)
            runtime.update(rpc=rpc, process=process, reader_task=reader_task)
            controller = Controller(args.data / "controller.sqlite", rpc,
                                    mcp_url=f"http://{args.host}:{args.port}")
            rpc.on_notification = controller.handle_notification
            rpc.on_request = controller.record_approval
            controller.recover_inflight()
            await controller.reconcile_inflight()
            await controller.resume_pending_splits()
            runtime.update(controller=controller, task=asyncio.create_task(scheduler(controller)))
        except Exception as exc:
            logging.getLogger(__name__).exception("Controller startup failed")
            runtime["error"] = str(exc)
        finally:
            runtime["ready"].set()

    async def shutdown():
        if task := runtime.get("task"):
            task.cancel()
        if process := runtime.get("process"):
            await stop_codex_process(process)

    app.on_startup(startup)
    app.on_shutdown(shutdown)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def local_api(path: str, request: Request):
        if request.method != "GET":
            if not trusted_origin(request.headers.get("origin"), args.port):
                return JSONResponse({"error": "untrusted browser origin"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse({"error": "JSON content type required"}, status_code=415)
        try:
            controller = await wait_for_controller(runtime)
            body = await request.json() if request.method != "GET" else {}
            status, result = await LocalAPI(controller).execute(
                request.method, "/api/" + path + ("?" + request.url.query if request.url.query else ""), body)
            return JSONResponse(result, status_code=status)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except Exception as exc:
            logging.getLogger(__name__).exception("API failed")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @ui.page("/")
    async def page():
        try:
            controller = await wait_for_controller(runtime)
        except RuntimeError as exc:
            ui.label("Agent Controller could not start").classes("text-xl font-bold")
            ui.label(str(exc))
            return
        sessions = controller.sessions()
        selected = {"id": sessions[0]["id"] if sessions else None,
                    "tab": "Chat", "signature": None, "drafts": {}}
        live = {"chat_refresh": None, "chat_revision": None, "chat_scroll": None,
                "scroll_percent": 1.0, "at_bottom": True}
        history_pagers = {}
        expanded = {}
        ui.add_head_html("""<style>
          :root { --surface:#18272b; --surface-raised:#203237; --line:#34494d;
                  --muted:#b7cbca; --accent:#a5e0bf; }
          body { background:#10191d; color:#f2f6f4; }
          .q-page-container { max-width:1600px; margin:auto; }
          .operator-header { border-bottom:1px solid var(--line); gap:10px; }
          .operator-panel { background:var(--surface); border-radius:14px; padding:18px; }
          .operator-muted { color:var(--muted); }
          .operator-button { background:var(--accent); color:#10251a; font-weight:700; }
          .operator-sidebar { position:sticky; top:78px; }
          .operator-graph { height:320px; }
          .operator-sessions { max-height:30vh; overflow-y:auto; }
          .operator-session { justify-content:flex-start; text-align:left; border-radius:9px; }
          .operator-session:hover, .operator-session:focus-visible { background:var(--surface-raised); }
          .operator-inspector { min-width:0; }
          .operator-chat { background:#132126; border:1px solid var(--line); border-radius:14px; }
          .operator-chat-scroll { height: min(43vh, 600px); min-height:260px; }
          .operator-turn { background:var(--surface); border:1px solid var(--line);
                           border-radius:12px; padding:16px; max-width:75ch; width:100%; }
          .operator-message { line-height:1.55; overflow-wrap:anywhere; white-space:pre-wrap; }
          .operator-code { max-width:100%; overflow-x:auto; }
          .operator-compose { background:var(--surface); border:1px solid var(--line);
                              border-radius:14px; padding:12px; }
          .operator-compose .q-textarea textarea { min-height:56px; }
          .q-btn:focus-visible, .q-tab:focus-visible, .q-expansion-item:focus-visible {
            outline:2px solid var(--accent); outline-offset:2px; }
          @media(max-width:800px) {
            .operator-sidebar { position:static; }
            .operator-graph { height:170px; }
            .operator-chat-scroll { height:42vh; min-height:240px; }
            .operator-header { justify-content:flex-start; }
          }
        </style>""")
        ui.dark_mode().enable()

        def show_error(exc):
            ui.notify(str(exc), color="negative")

        async def act(coroutine):
            try:
                await coroutine
                update()
            except Exception as exc:
                show_error(exc)

        def apply_sync(action):
            try:
                action()
                update()
            except Exception as exc:
                show_error(exc)

        def render_approval_details(approval):
            params = json.loads(approval["params"])
            turn = controller.db.one("SELECT id FROM turns WHERE codex_turn_id=?", (params.get("turnId"),))
            items = controller.turn_items(turn["id"]) if turn else []
            changes = matching_file_changes(params.get("itemId"), items)
            if changes:
                ui.label(f"Proposed changes to {len(changes)} files")
                for change in changes:
                    with ui.expansion(f"{change.get('kind', {}).get('type', 'change')} · {change.get('path', 'unknown path')}"):
                        ui.code(change.get("diff", "No diff supplied"))
            elif params.get("command"):
                if params.get("reason"):
                    ui.label(params["reason"])
                ui.label(f"Working directory: {params.get('cwd') or 'unspecified'}")
                ui.code(params["command"])
            else:
                ui.code(approval["params"])

        def choose(session_id):
            selected["id"] = session_id
            selected["tab"] = "Chat"
            live["chat_revision"] = None
            live["scroll_percent"] = 1.0
            live["at_bottom"] = True
            inspector.refresh()

        def on_graph_click(event):
            node_id = graph_clicked_session_id(event)
            if node_id:
                choose(node_id)

        with ui.header().classes("operator-header items-center bg-[#142126] px-4 py-2"):
            ui.label("Agent Controller").classes("text-xl font-bold mr-auto")
            status_label = ui.label()
            ui.button("Pause New Prompts", on_click=lambda: act(controller.pause())).props("outline")
            ui.button("Resume Dispatch", on_click=lambda: act(controller.resume())).props("outline")
            ui.button("Stop All Running", on_click=lambda: act(controller.stop_all())).props("outline color=negative")

        @ui.refreshable
        def approval_banner():
            for approval in controller.db.many("SELECT * FROM approvals WHERE decision IS NULL ORDER BY rowid"):
                session = controller.db.one("SELECT id,title FROM sessions WHERE thread_id=?", (approval["thread_id"],))
                if session:
                    with ui.row().classes("w-full items-center justify-between p-3 bg-amber-900"):
                        ui.label(f"Approval needed for {session['title']}: {approval['method']}")
                        ui.button("Review changes", on_click=lambda sid=session["id"]: choose(sid)).props("outline")

        approval_banner()

        with ui.row().classes("w-full items-start gap-5 p-4 flex-wrap"):
            with ui.column().classes("operator-sidebar w-full md:w-[34%] gap-3"):
                ui.label("Session graph").classes("text-lg font-semibold")
                chart = ui.echart(graph_options(controller.sessions(), controller.graph()),
                                  on_point_click=on_graph_click).classes("operator-graph w-full operator-panel")
                search = ui.input("Find a session").props("outlined dense clearable").classes("w-full")

                @ui.refreshable
                def session_list():
                    query = (search.value or "").lower()
                    with ui.column().classes("operator-sessions w-full gap-1"):
                        for session in controller.sessions():
                            if query in (session["title"] + " " + session["status"]).lower():
                                ui.button(f"{session['title']} · {session['status']}",
                                          on_click=lambda sid=session["id"]: choose(sid)).props("flat no-caps").classes("operator-session w-full")

                search.on("update:model-value", lambda _e: session_list.refresh())
                session_list()

                with ui.expansion("Create session").classes("w-full operator-panel"):
                    title = ui.input("Title").classes("w-full")
                    objective = ui.textarea("Objective").classes("w-full")
                    cwd = ui.input("Working directory", value=str(Path.cwd())).classes("w-full")

                    async def create():
                        try:
                            sid = await controller.create_session(title.value, objective.value, cwd.value)
                            choose(sid)
                            update()
                        except Exception as exc:
                            show_error(exc)

                    ui.button("Create session", on_click=create).classes("operator-button")

            with ui.column().classes("operator-inspector w-full md:flex-1 min-w-[300px] gap-3"):
                @ui.refreshable
                def inspector():
                    live["chat_refresh"] = None
                    live["chat_scroll"] = None
                    sid = selected["id"]
                    session = controller.session(sid) if sid else None
                    if not session:
                        selected["signature"] = None
                        ui.label("Create or select a session to inspect its work.").classes("operator-muted")
                        return
                    active = next((t for t in controller.turns(sid) if t["status"] in ("running", "waiting_for_approval", "interrupt_requested")), None)
                    pending_approvals = controller.db.one("SELECT count(*) AS n FROM approvals WHERE thread_id=? AND decision IS NULL",
                                                          (session["thread_id"],))["n"]
                    pending_work = controller.db.one("SELECT count(*) AS n FROM work_items WHERE session_id=? AND status='queued'", (sid,))["n"]
                    selected["signature"] = (session["title"], session["status"], session["held"],
                                             active["id"] if active else None, pending_approvals, pending_work)
                    ui.label(session["title"]).classes("text-2xl font-semibold")
                    ui.label(f"{session['status']} · {sid}").classes("operator-muted text-sm break-all")
                    with ui.row().classes("gap-2"):
                        if session["status"] == "archived":
                            ui.label("Archived sessions are read only.").classes("operator-muted")
                        elif session["held"]:
                            ui.button("Resume Session", on_click=lambda: (controller.resume_session(sid), update()))
                        else:
                            ui.button("Hold Session", on_click=lambda: (controller.hold_session(sid), update()))
                        if session["status"] != "archived":
                            ui.button("Archive", on_click=lambda:
                                      apply_sync(lambda: controller.archive_session(sid))).props("outline")
                        if active:
                            ui.button("Stop Turn", on_click=lambda t=active: act(controller.stop_turn(t["id"]))).props("color=negative")
                    tabs = ui.tabs().classes("w-full")
                    with tabs:
                        for name in ("Chat", "Overview", "Schedule", "Activity", "Requests", "Logs"):
                            ui.tab(name)
                    tabs.value = selected["tab"]

                    def render_tab():
                        selected["tab"] = tabs.value
                        live["chat_refresh"] = None
                        tab_body.refresh()

                    tabs.on("update:model-value", lambda _e: render_tab())

                    @ui.refreshable
                    def tab_body():
                        tab = selected["tab"]
                        if tab == "Chat":
                            def remember_expansion(key, event):
                                expanded[(sid, key)] = event.value

                            def scroll_changed(event):
                                live["scroll_percent"] = event.vertical_percentage
                                live["at_bottom"] = event.vertical_percentage >= 0.95

                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label("Conversation").classes("text-lg font-semibold")
                                ui.button("Jump to latest", icon="south", on_click=lambda:
                                          live["chat_scroll"].scroll_to(percent=1.0, duration=0.2)).props("flat no-caps")

                            @ui.refreshable
                            def chat_messages():
                                stale = controller.db.many("SELECT id,method,created_at FROM approvals WHERE thread_id=? AND decision='stale_after_restart' ORDER BY rowid",
                                                           (session["thread_id"],))
                                if stale and session["status"] == "held":
                                    with ui.card().classes("w-full operator-panel border border-amber-400"):
                                        ui.label("Approval connection was lost on restart").classes("text-lg font-semibold")
                                        ui.label("This turn is held for reconciliation. Its old approval cannot be answered through this process.")
                                approvals = controller.db.many("SELECT * FROM approvals WHERE thread_id=? AND decision IS NULL ORDER BY rowid",
                                                               (session["thread_id"],))
                                for approval in approvals:
                                    with ui.card().classes("w-full operator-panel border border-amber-400"):
                                        ui.label("Waiting for your approval").classes("text-lg font-semibold")
                                        ui.label(approval["method"])
                                        render_approval_details(approval)
                                        if approval["method"] in SIMPLE_APPROVAL_METHODS:
                                            with ui.row():
                                                ui.button("Approve", on_click=lambda aid=approval["id"]:
                                                          act(controller.resolve_approval(aid, "accept")))
                                                ui.button("Decline", on_click=lambda aid=approval["id"]:
                                                          act(controller.resolve_approval(aid, "decline"))).props("color=negative")
                                        else:
                                            ui.label("This approval needs a specialized response.").classes("operator-muted")
                                if sid not in history_pagers:
                                    history_pagers[sid] = HistoryPager(lambda before: controller.turns_page(sid, 50, before))
                                pager = history_pagers[sid]
                                pager.refresh_latest()
                                if pager.has_more:
                                    ui.button("Load older turns", on_click=lambda:
                                              (pager.load_older(), chat_messages.refresh())).props("flat no-caps").classes("self-center")
                                for turn in pager.rows():
                                    work = controller.work_item(turn["work_item_id"])
                                    with ui.column().classes("operator-turn"):
                                        with ui.row().classes("w-full items-center justify-between gap-2"):
                                            ui.label(f"{work['source'] if work else 'prompt'} · {turn['status']}").classes("font-semibold text-[#a5e0bf]")
                                            ui.label(turn["started_at"] or "pending").classes("operator-muted text-xs")
                                        if work and work["payload"]:
                                            with ui.expansion("Prompt", value=expanded.get((sid, (turn["id"], "prompt")), False),
                                                              on_value_change=lambda e, key=(turn["id"], "prompt"): remember_expansion(key, e)).classes("w-full"):
                                                ui.label(work["payload"]).classes("operator-message")
                                        for item in display_items(controller.turn_items(turn["id"])):
                                            payload = item["payload"]
                                            if item["type"] == "agentMessage" and item["text"]:
                                                ui.label(item["text"] + (" ▍" if item["partial"] else "")).classes("operator-message")
                                            else:
                                                key = (turn["id"], item["id"])
                                                with ui.expansion(f"{item['type']} · {item['created_at']}",
                                                                  value=expanded.get((sid, key), False),
                                                                  on_value_change=lambda e, key=key: remember_expansion(key, e)).classes("w-full"):
                                                    ui.code(json.dumps(payload, indent=2)).classes("operator-code")
                                        reflection = controller.db.one("SELECT * FROM reflections WHERE worker_turn_id=?", (turn["id"],))
                                        if reflection:
                                            key = (turn["id"], "reflection")
                                            with ui.expansion(f"Reflection · {reflection['status']}",
                                                              value=expanded.get((sid, key), False),
                                                              on_value_change=lambda e, key=key: remember_expansion(key, e)).classes("w-full"):
                                                ui.code(reflection["analysis"] or "Pending").classes("operator-code")
                                                for item in controller.reflection_items(reflection["id"]):
                                                    item_key = (turn["id"], "reflection", item["id"])
                                                    with ui.expansion(f"{item['type']} · {item['created_at']}",
                                                                      value=expanded.get((sid, item_key), False),
                                                                      on_value_change=lambda e, key=item_key: remember_expansion(key, e)):
                                                        ui.code(json.dumps(item["payload"], indent=2)).classes("operator-code")
                                                if reflection["status"] in ("failed", "interrupt_failed"):
                                                    ui.button("Retry reflection", on_click=lambda rid=reflection["id"]:
                                                              apply_sync(lambda: controller.retry_reflection(rid)))
                                        if turn["status"] in ("failed", "interrupted"):
                                            ui.button("Retry turn", on_click=lambda tid=turn["id"]: act(controller.retry_turn(tid)))
                            with ui.scroll_area(on_scroll=scroll_changed).classes("operator-chat operator-chat-scroll w-full") as chat_scroll:
                                with ui.column().classes("w-full gap-3 p-4"):
                                    chat_messages()
                            live["chat_scroll"] = chat_scroll
                            live["chat_refresh"] = chat_messages.refresh
                            live["chat_revision"] = conversation_revision(controller.db, sid)
                            ui.timer(0.2, lambda: chat_scroll.scroll_to(percent=live["scroll_percent"]), once=True)
                            if session["status"] != "archived":
                                with ui.column().classes("operator-compose w-full gap-2"):
                                    prompt = ui.textarea("Prompt", value=selected["drafts"].get(sid, "")).props("outlined autogrow").classes("w-full")
                                prompt.on("update:model-value", lambda _e: selected["drafts"].__setitem__(sid, prompt.value))

                                async def send_prompt():
                                    try:
                                        await controller.submit(sid, prompt.value)
                                        selected["drafts"].pop(sid, None)
                                        update()
                                    except Exception as exc:
                                        show_error(exc)

                                with ui.row().classes("w-full justify-end"):
                                    ui.button("Send prompt", icon="send", on_click=send_prompt).classes("operator-button")
                        elif tab == "Overview":
                            ui.label(session["objective"])
                            ui.label(session["summary"] or "No reflection yet").classes("operator-muted")
                            ui.label(f"Session ID: {sid}")
                            ui.label(f"Codex thread: {session['thread_id']}")
                            ui.label(f"Working directory: {session['cwd']}")
                            ui.label(f"Context tokens: {session['context_tokens']} / {session['context_window'] or 'unknown'}")
                            with ui.expansion("Scope contract"):
                                ui.code(session["scope"])
                            ui.label("Relationships").classes("text-lg font-semibold")
                            for edge in controller.graph():
                                if sid in (edge["source_id"], edge["target_id"]):
                                    other_id = edge["target_id"] if edge["source_id"] == sid else edge["source_id"]
                                    other = controller.session(other_id)
                                    with ui.row().classes("items-center"):
                                        ui.label(f"{edge['type']} · {other['title'] if other else other_id}")
                                        ui.button("Remove", on_click=lambda eid=edge["id"]:
                                                  apply_sync(lambda: controller.remove_edge(eid))).props("flat")
                            targets = {s["id"]: s["title"] for s in controller.sessions() if s["id"] != sid and s["status"] != "archived"}
                            if targets:
                                target = ui.select(targets, label="Link to session").classes("w-full")
                                relationship = ui.select(["PARENT_OF", "SIBLING_OF", "DEPENDS_ON", "RELATED_TO"],
                                                         label="Relationship", value="RELATED_TO").classes("w-full")
                                ui.button("Add relationship", on_click=lambda:
                                          apply_sync(lambda: controller.add_edge(sid, target.value, relationship.value)))
                            with ui.expansion("Fork session"):
                                fork_title = ui.input("New title").classes("w-full")
                                fork_objective = ui.textarea("New objective").classes("w-full")

                                async def create_fork():
                                    try:
                                        new_id = await controller.fork_session(sid, fork_title.value, fork_objective.value)
                                        choose(new_id)
                                        update()
                                    except Exception as exc:
                                        show_error(exc)

                                ui.button("Create fork", on_click=create_fork)
                        elif tab == "Schedule":
                            schedule = controller.db.one("SELECT * FROM cron_schedules WHERE session_id=?", (sid,))
                            expression = ui.input("Cron expression", value=schedule["expression"] if schedule else "0 9 * * *")
                            timezone = ui.input("Timezone", value=schedule["timezone"] if schedule else "UTC")
                            enabled = ui.switch("Enabled", value=bool(schedule["enabled"]) if schedule else True)
                            ui.button("Save schedule", on_click=lambda:
                                      apply_sync(lambda: controller.set_cron(sid, expression.value, timezone.value, enabled.value)))
                            if schedule:
                                for field in ("next_due", "last_due", "last_started", "last_finished", "last_outcome"):
                                    ui.label(f"{field.replace('_', ' ').title()}: {schedule[field] or 'Never'}")
                        elif tab == "Activity" or tab == "Logs":
                            for event in controller.events(sid):
                                ui.label(f"{event['created_at']} · {event['type']}")
                                if tab == "Logs":
                                    ui.code(event["payload"])
                        else:
                            for item in controller.work_items(sid):
                                if item["status"] == "queued":
                                    with ui.row().classes("items-center"):
                                        ui.label(f"{item['source']}: {item['payload']}")
                                        ui.button("Cancel", on_click=lambda wid=item["id"]:
                                                  apply_sync(lambda: controller.cancel_work_item(wid))).props("flat")
                            for update in controller.passive_inbox(sid):
                                ui.label(f"{update['title']} · revision {update['revision']}: {update['summary']}")
                            for request in controller.db.many("SELECT * FROM agent_requests WHERE source_id=? OR target_id=? ORDER BY rowid", (sid, sid)):
                                with ui.card().classes("w-full operator-panel"):
                                    ui.label(f"Request · {request['status']}").classes("operator-muted")
                                    ui.label(request["question"])
                                    if request["reply"]:
                                        ui.label(request["reply"])
                                    elif request["target_id"] == sid and request["status"] == "queued":
                                        answer = ui.textarea("Reply").classes("w-full")
                                        ui.button("Send reply", on_click=lambda rid=request["id"], field=answer:
                                                  act(controller.reply(rid, field.value, sid)))
                            for approval in controller.db.many("SELECT * FROM approvals WHERE thread_id=? AND decision IS NULL ORDER BY rowid", (session["thread_id"],)):
                                with ui.card().classes("w-full operator-panel"):
                                    ui.label(f"Approval · {approval['method']}")
                                    render_approval_details(approval)
                                    if approval["method"] in SIMPLE_APPROVAL_METHODS:
                                        ui.button("Approve", on_click=lambda aid=approval["id"]:
                                                  act(controller.resolve_approval(aid, "accept")))
                                        ui.button("Decline", on_click=lambda aid=approval["id"]:
                                                  act(controller.resolve_approval(aid, "decline"))).props("color=negative")
                                    else:
                                        ui.label("This approval requires a specialized response.").classes("operator-muted")

                    tab_body()

                inspector()

        def update(refresh_inspector=True):
            status = controller.status()
            status_label.set_text(f"{'Paused' if status['paused'] else 'Running'} · {status['running']} active · {status['queued']} queued · {status['pending_approvals']} approvals")
            refresh_chart(chart, graph_options(controller.sessions(), controller.graph()))
            approval_banner.refresh()
            session_list.refresh()
            sid = selected["id"]
            session = controller.session(sid) if sid else None
            active = next((t for t in controller.turns(sid) if t["status"] in ("running", "waiting_for_approval", "interrupt_requested")), None) if session else None
            pending_approvals = controller.db.one("SELECT count(*) AS n FROM approvals WHERE thread_id=? AND decision IS NULL",
                                                  (session["thread_id"],))["n"] if session else 0
            pending_work = controller.db.one("SELECT count(*) AS n FROM work_items WHERE session_id=? AND status='queued'",
                                             (sid,))["n"] if session else 0
            signature = (session["title"], session["status"], session["held"],
                         active["id"] if active else None, pending_approvals, pending_work) if session else None
            if refresh_inspector or signature != selected["signature"]:
                inspector.refresh()
            elif live["chat_refresh"] and conversation_revision(controller.db, sid) != live["chat_revision"]:
                live["chat_refresh"]()
                live["chat_revision"] = conversation_revision(controller.db, sid)
                if live["at_bottom"] and live["chat_scroll"]:
                    live["chat_scroll"].scroll_to(percent=1.0)

        ui.timer(1.5, lambda: update(False))
        ui.context.client.on_connect(lambda: live["chat_scroll"] and
                                     live["chat_scroll"].scroll_to(percent=1.0))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ui.run(host=args.host, port=args.port, title="Agent Controller", reload=False, dark=True)


if __name__ == "__main__":
    main()
