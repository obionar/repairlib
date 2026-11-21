# Contributing to RepairLib

Bug reports, documentation fixes, translations, and tests are welcome. For a
larger change, open an issue first so the scope can be agreed before you code.
Report ordinary bugs with the tested commit, platform, steps using synthetic
data, and what you expected versus what happened. Never post customer data,
database files, backups, credentials, or tokens. Report suspected security
vulnerabilities privately to the maintainers, not in a public issue.

1. Follow the [source setup](README.md#requirements), then create a
   [disposable demo workspace](docs/operations.md#initialize-an-isolated-synthetic-demo).
2. Keep the change focused. Put shared behavior in `src/repairlib/domain/`,
   SQLite access in `src/repairlib/storage/`, and keep adapters thin. Add a
   numbered migration rather than editing an existing one.
3. Add or update tests for changed behavior. Run `python3 scripts/check.py`
   before submitting a pull request. For a focused check, use
   `python3 scripts/check.py --module test_operations_doc.py`, replacing the
   filename with the module you need. For template, CSS or page-structure work,
   read the [web frontend map](docs/frontend.md) first and run
   `python3 scripts/ui_smoke.py --password-file <workspace-password-file>` against
   a disposable workspace; it checks the documented hooks at desktop and phone
   widths with JavaScript disabled.
   If you change a browser route, HTTP API, CLI command or MCP tool on purpose,
   `tests/test_web_route_golden.py`, `tests/test_surface_golden.py` or `tests/test_cli_repairs_golden.py`
   may fail until you regenerate the corresponding file in `tests/golden/` with
   `UPDATE_GOLDEN=1 python3 scripts/check.py --module test_web_route_golden.py`
   (or the affected module). Review that diff like any interface change; an accidental diff means the
   change leaked into a public surface.
4. In the pull request, explain why the change is needed and which checks ran.

See the [project map](README.md#project-map) for where to start. If multiple
agents or shells might edit the same checkout, use separate clones or run the
editing shell in the foreground under `python3 -B scripts/writer_lock.py -- "$SHELL"`
on Unix. This protects source edits, not a running workshop workspace.