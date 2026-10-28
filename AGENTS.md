# Repository Guidelines

## Project Structure & Module Organization

`AGENT_CONTROLLER_SPEC.md` defines architecture and acceptance criteria. Python services live in `app/`; the standard browser UI is `app/static/index.html`, and NiceGUI graph code is in `app/ui/`. Protocol schemas are generated under `schemas/codex-app-server/`; tests live in `tests/`. Runtime SQLite files belong in Git-ignored `data/`.

## Build, Test, and Development Commands

Use Python 3.12+ and Codex CLI 0.165.0. Install with `python3 -m pip install -e '.[dev]'` (add `ui` for NiceGUI). Run locally with `python3 -m app.main --port 8765 --data ./data` or `python3 -m app.nicegui_main` when NiceGUI is installed. Run tests with `python3 -m pytest -q`. Regenerate protocol definitions with `codex app-server generate-json-schema --out ./schemas/codex-app-server` after upgrading Codex.

## Coding Style & Naming Conventions

Use four-space indentation and Python `snake_case` for modules, functions, and variables; use `PascalCase` for classes. Keep controller state changes in Python services with event journal entries and SQLite projections, rather than direct writes from agent tools. Name tests `test_*.py`. No formatter or linter is configured yet.

## Testing Guidelines

Follow test driven development for each behavior change: write a focused failing test, confirm the failure, implement the smallest passing change, then refactor with tests green. Section 21 of the specification defines acceptance behavior. Prioritize tests for the dispatch gate, interruption, queue ordering, reflection isolation, neighbor authorization, and crash recovery. Fake Codex transports should test state transitions; real App Server smoke tests should verify protocol compatibility. No coverage threshold is configured.

## Commit & Pull Request Guidelines

No Git history is available here, so there is no established commit-message convention. Use short, imperative subjects, such as `Add durable work queue`. Pull requests should link the relevant specification section, describe the failing test added before implementation, list tests run, and include screenshots for UI changes.

## Security & Configuration

Keep runtime data and secrets out of commits. Bind the local UI to loopback by default; require authentication before exposing it beyond the host. Preserve user pause and stop controls across restarts and never let agent-generated commands override them.
