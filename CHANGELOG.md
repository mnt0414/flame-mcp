# Changelog

All notable changes to flame-mcp are recorded here.

## Unreleased

## 0.1.0

First public release.

- Lets an MCP client, such as Claude Code, read and edit a running Autodesk Flame. There are 93 operations in 13 categories: reading, organizing, editing, Batch, and multi-step plans (`plan.run`). See [docs/ops.md](docs/ops.md) for the full list.
- Records each operation that it runs. You can undo most changes with `journal.revert`; some changes can't be undone. See [Undo](README.md#undo).
- Tested with Flame 2025.2.4 and 2026.2.3 on macOS. It also starts on other Flame versions and shows a warning.
