# Contributing to flame-mcp

Thank you for your interest in flame-mcp. This page explains how to report a problem, propose a change, and prepare a pull request.

flame-mcp is maintained by one person in spare time. Issues and pull requests are handled on a best-effort basis, so a reply can take a while.

## Report a bug or ask a question

Use [GitHub Issues](https://github.com/mnt0414/flame-mcp/issues) for bug reports, questions, and requests.

For a bug report, include:

- Your macOS version, Flame version, and Python version
- The operation (op) that you ran and its arguments
- The response that you got: the response envelope, and the `errorCode` if there is one
- What you expected to happen
- For a connection problem, the output of `scripts/doctor.sh` (see [Troubleshooting](README.md#troubleshooting))

Remove anything that you don't want to make public. Logs and responses can contain file paths and project names.

> [!IMPORTANT]
> **Don't report a security vulnerability in a public issue, pull request, or discussion.** Follow [.github/SECURITY.md](.github/SECURITY.md) and use GitHub's private vulnerability reporting.

## Propose a change

1. Open an issue and describe the change. Discuss it there before you open a pull request.
2. Keep each pull request to one topic. Send unrelated changes as separate pull requests.
3. Make sure the checks in the next section pass before you open the pull request. CI runs the same checks on every pull request.

## Set up and run the checks

Use Python 3.11 or later. From the root of the repository, run these commands:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check
.venv/bin/ruff format --check
.venv/bin/mypy
.venv/bin/python -m pytest
```

All of them must pass. The unit tests need no Flame. The tests of the shell scripts need `shellcheck`, so install it first.

The regression tests on a real Flame (`scripts/run_e2e.sh` for one test, `scripts/regress.sh` for all of them) need Flame. They change the project that they run in, so they run only in a dedicated test project named `flame_mcp_probe`. For setup and instructions, see [docs/testing.md](docs/testing.md).

## Test policy

New functionality comes with tests.

- A new operation, or a change to what an operation does, comes with unit tests in `tests/`. The unit tests run against a fake Flame, so they need no Flame.
- A bug fix comes with a test that fails without the fix.
- A new regression test on a real Flame needs three steps:
  1. In the docstring of the script, write one paragraph that starts with `Checks:`. Write only one.
  2. Add a row for the test to `e2e_table` in `scripts/run_e2e.sh`.
  3. Update the client count in `tests/test_e2e_guard.py`.

## Rules for code

### How to structure a change

- Add a feature as an operation. The MCP tools stay four: `flame_version_info`, `flame_catalog`, `flame_do`, and `flame_context`.
- Start the name of an operation with its category, for example `segment.trim`.
- Declare `effect` and `undo` for every operation. Write the Flame API names that the generated code uses in `requires`. A name that isn't in the symbol table is a registration error.
- A write operation follows this order: record the intent in the journal, write to Flame, read the result back from Flame to decide what happened, and record the outcome. If the write had no effect, return `SILENT_NOOP`.
- Two documents are generated: `docs/ops.md` and `docs/testing.md`. Don't edit them by hand. After you change an operation, or the docstring of a regression test, or `scripts/run_e2e.sh`, regenerate them:

  ```bash
  .venv/bin/python scripts/build_ops_index.py
  .venv/bin/python scripts/build_testing_doc.py
  ```

- When you add a member to a `Literal` in `types.py`, update every `match` that handles it. `tests/test_exhaustiveness.py` checks this.
- When you change the protocol between the server and the bridge, raise `BRIDGE_ABI`.
- Files that the server and the bridge create must be mode 0600, and directories must be mode 0700.
- `INSTRUCTIONS` in `server.py` must stay at 2048 bytes or less.
- Keep the versions of the dev tools in `pyproject.toml` pinned. Don't commit a `uv.lock` file.

### What not to do

- Don't send a non-ASCII character to Flame. This includes the Python files that are sent to Flame, which must be ASCII only.
- Don't call the Flame API from any thread except the main thread. This includes `schedule_idle_event`.
- Don't use a loop or a comprehension in the code that a write operation generates.
- Don't use Flame's Undo, including `execute_shortcut`. A write must keep `undoSteps` at 0.
- Don't retry a write automatically after `TIMEOUT` or `BRIDGE_LOST`, because the result is unknown.
- Don't add code that deletes or moves anything inside a library.
- Don't write anything but JSON-RPC to stdout.
- Don't import `flame` or `PySide6` at the top level of a module.
- Don't use syntax from Python 3.10 or later in `stub/flame_mcp_bridge.py`: no `match`, and no `X | Y` at run time. Older Flame versions load this file with an older Python.
- Don't put the server's files in `/tmp`.
- Don't run a real-Flame regression test in any project except `flame_mcp_probe` (or a project named `flame_mcp_probe_*`).
- Don't set the fault-injection variables `FLAME_MCP_TEST_*` in the environment of a server registration.

## Documentation

- Write documentation in English. The one exception is `README.ja.md`, the Japanese version of `README.md`.
- `README.md` and `README.ja.md` must say the same thing. When you change one, change the other.
- Don't edit `docs/ops.md` or `docs/testing.md` by hand (see above).

## License

flame-mcp is released under the MIT License. Your contribution is licensed under the same license. See [LICENSE](LICENSE).
