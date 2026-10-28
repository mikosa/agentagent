# Agent Controller

A local Python service for controller-owned Codex sessions. The design baseline is [AGENT_CONTROLLER_SPEC.md](AGENT_CONTROLLER_SPEC.md). The current implementation includes a SQLite event journal, priority work queues, global pause and stop controls, a browser graph and session inspector, cron schedules, an MCP bridge, reflection summaries, and scoped split successors.

## Requirements

- Python 3.12 or newer and `jsonschema` (`python3 -m pip install -e '.[dev]'`)
- Codex CLI 0.165.0, signed in and available as `codex`
- A writable Codex home. If the default home is read-only, set `CODEX_HOME` to a writable location that has valid Codex authentication.

The standard library browser service binds to `127.0.0.1` and does not require a frontend build. A NiceGUI/ECharts entry point is also included; install its optional dependencies with `python3 -m pip install -e '.[ui,dev]'`.

## Run

```sh
python3 -m app.main --port 8765 --data ./data
```

Open `http://127.0.0.1:8765`. Create a session with an absolute working directory, then send a prompt. Runtime state is stored under `data/`, which is ignored by Git.

To use the Python-defined NiceGUI interface instead, run `python3 -m app.nicegui_main --port 8765 --data ./data` after installing the `ui` extra. The two entry points use the same database and must not run simultaneously.

## Test

```sh
python3 -m pytest -q
codex app-server generate-json-schema --out ./schemas/codex-app-server
```

Tests use a fake Codex transport for queue, stop, graph, reflection, split, and recovery behavior. The checked-in schema was generated from Codex CLI 0.165.0. Regenerate it and rerun tests when upgrading Codex.

Create a consistent SQLite backup, including committed WAL changes, with `python3 -m app.backup --data ./data --out ./controller-backup.sqlite`. The destination must not already exist.

## Current limits

The implementation is still short of the complete specification. The standard interface uses a lightweight SVG graph and polling; the NiceGUI/ECharts entry point still needs a connected browser check. Some advanced inspector controls remain incomplete. Split plans recover recorded successors, but an uncertain external thread start requires inspection before retrying. Crash recovery holds uncertain turns and reflections and backfills known Codex items. Full end-to-end prompt execution needs verification with Codex model access.
