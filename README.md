# flame-mcp

English | [日本語](./README.ja.md)

**Let an AI assistant read and edit your Autodesk Flame projects through the Model Context Protocol (MCP).**

flame-mcp connects an MCP client, such as Claude Code, to Flame while Flame is running. Describe the task in natural language. The assistant finds a matching operation (op) and runs it in Flame. flame-mcp records each operation that it runs, and you can undo most changes later. Some changes can't be undone (see [Undo](#undo)).

> [!NOTE]
> **Status: pre-release.** flame-mcp has been tested with **Flame 2025.2.4 and 2026.2.3 on macOS**. It also starts on other Flame versions and shows a warning, but those versions haven't been tested. This is an independent project by one person. It isn't made, endorsed, or supported by Autodesk, and it isn't related to [abrahamADSK/flame-mcp](https://github.com/abrahamADSK/flame-mcp), which has the same name.

[What it does](#what-it-does) · [Before you start](#before-you-start) · [Quick start](#quick-start) · [Safety and privacy](#safety-and-privacy) · [Undo](#undo) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Limitations](#limitations) · [How it works](#how-it-works) · [Documentation](#documentation) · [Questions, requests, and bug reports](#questions-requests-and-bug-reports)

## What it does

flame-mcp works best for organizing projects and for routine tasks that follow fixed steps.

| Action | Examples |
| --- | --- |
| Read | List libraries, reels, clips, and sequences. Read segments, Batch nodes, and their attributes. |
| Organize | Create, duplicate, move, and rename reels, clips, and sequences. Delete clips and sequences. |
| Edit | Trim, slip, and replace segments. Add and set up TimelineFX. |
| Batch | Create, connect, and set up nodes. Save and load setups. Render. |
| Multi-step plans | Run several operations in order in one call, and undo them together (`plan.run`). |

There are 84 operations in 13 categories. For the full list, see [docs/ops.md](docs/ops.md).

## Before you start

> [!CAUTION]
> **The assistant changes your Flame project directly.** Try it on a test project first. Before you let the assistant change an important project, archive the project.
>
> What the assistant reads is sent to the AI service that your client uses. For details on what is sent, see [Information sent to the AI service](#information-sent-to-the-ai-service). Before you use flame-mcp on a project under an NDA, check the AI service's data retention policy and your client's logs.

## Quick start

### Requirements

| | |
| --- | --- |
| OS | macOS |
| Flame | 2025.2.4, 2026.2.3 |
| Python | 3.11 or later (for the server) |
| MCP client | A client that can run a local stdio server. Tested with Claude Code. |

flame-mcp sends only ASCII characters to Flame. Use ASCII characters in the names and file paths that you ask the assistant to use. You can talk to the assistant in any language.

**1. Get the code and install the server.**

```bash
git clone https://github.com/mnt0414/flame-mcp.git
cd flame-mcp
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

Replace `python3.12` with any installed Python 3.11 or later. The `/usr/bin/python3` that comes with macOS can be older than 3.11.

**2. Quit Flame, then install the bridge into Flame.**

The installer copies two files:

- `flame_mcp_bridge.py`: the entry point that Flame loads when it starts. It's installed in `/opt/Autodesk/shared/python/`.
- `impl.py`: the main part of the bridge. It's installed in `~/Library/Application Support/flame-mcp/bridge/`.

If a file with the same name already exists, the installer backs it up before it overwrites the file.

```bash
scripts/install_bridge.sh --dry-run
scripts/install_bridge.sh
scripts/doctor.sh
```

With `--dry-run`, the installer writes nothing and only shows what it would change. If the output looks right, run `install_bridge.sh`. `doctor.sh` checks that the installation worked.

**3. Register the server with your MCP client.**

For Claude Code, run this command:

```bash
claude mcp add flame -- /path/to/flame-mcp/scripts/flame_mcp_server.sh
```

<details>
<summary>Set it up with JSON</summary>

For a client that uses a JSON settings file, put the path of `flame_mcp_server.sh` in `command`. After you edit the file, quit the client completely and start it again.

```json
{
  "mcpServers": {
    "flame": {
      "command": "/path/to/flame-mcp/scripts/flame_mcp_server.sh"
    }
  }
}
```

</details>

**4. Check the connection.**

Start Flame, open a project, and give the assistant a simple request that changes nothing:

> "What is the name of the project that's open in Flame? Don't change anything in the project."

If the assistant tells you the project name, the connection works. If it doesn't, see [Troubleshooting](#troubleshooting).

### Example requests

Start with requests that only read. Then try a change whose result you can check quickly.

> "List the reels on the Desktop and the clips in each one."

> "In this sequence, replace the third segment on V1 with the clip I just imported. Match the source timecode to the original."

> "Extract each segment of this sequence to a reel, with 10-frame handles."

> "Load this setup into the current Batch group, and tell me the frame range before you render."

Requests that name the target, the action, and how to check the result usually work better than vague requests such as "make it better."

### Uninstall

Quit Flame, then run these commands:

```bash
scripts/uninstall_bridge.sh --dry-run
scripts/uninstall_bridge.sh
```

The script removes two files, `flame_mcp_bridge.py` and `impl.py`. It backs them up before it removes them.

The change records, the files saved for undo, and the logs stay. For file locations and retention periods, see [Files that flame-mcp keeps](#files-that-flame-mcp-keeps). You can delete these files manually if you no longer need them.

The server's registration in your MCP client also stays. In Claude Code, remove it with this command:

```bash
claude mcp remove flame
```

## Safety and privacy

### Information sent to the AI service

flame-mcp itself sends nothing over the internet. It doesn't collect usage data (telemetry).

What the assistant receives from flame-mcp becomes part of the conversation, and your MCP client sends it to the AI service. This includes the following:

- Names of projects, libraries, reels, clips, sequences, and nodes
- Parameter values of Batch nodes (numbers, and also text such as node names and the output path of a Write File node)
- File paths (they can include your user name)
- Metadata such as timecode
- Lines from Flame's logs

No flame-mcp operation reads images, except `eval.run`. When `eval.run` is turned on, the assistant can read any file, including images.

If your client uses a model that runs on your computer, the conversation can stay on your computer. Whether the client sends anything else depends on the client and its settings.

### Asking before an operation runs

Your MCP client's settings decide whether the assistant asks you before it runs an operation. On the flame-mcp side, you can limit which operations the assistant can use (see [Configuration](#configuration)). flame-mcp also checks every call before it sends anything to Flame.

### Text in a project that steers the assistant

Clip names, metadata, the contents of setups, and Flame's logs are passed to the assistant as they are. If they contain text that reads like instructions to an AI, the assistant might follow those instructions (prompt injection). To look through a project that you received from someone else, use [read-only mode](#read-only-mode).

### Safeguards in flame-mcp

- flame-mcp records each change before it sends the change to Flame.
- Operations that overwrite files (`overwrite=true`, or a render onto existing frames) are treated as destructive. If `destructive` isn't in `FLAME_MCP_ALLOW_EFFECTS`, flame-mcp refuses them. flame-mcp also tells the assistant to ask you before it runs an operation that overwrites or can't be undone. flame-mcp can't check that you agreed, so this depends on the assistant and your client.
- `journal.purge_checkpoints` deletes the copies saved for undo. It first runs as a dry run, and it deletes only when it receives the token that the dry run showed.
- If a call ends without an answer (`TIMEOUT`, `BRIDGE_LOST`), flame-mcp refuses changes until the assistant has checked the actual state.
- flame-mcp sends only ASCII characters to Flame. If a name, path, or piece of code contains any other character, flame-mcp refuses the call and sends nothing.
- `eval.run`, which runs arbitrary Python code, is turned off by default (see [Running arbitrary Python code](#running-arbitrary-python-code-evalrun)).

### Other programs running as the same user

The bridge accepts connections only from the user who started Flame, and it runs the code that it receives. flame-mcp's settings limit only the operations that come through the MCP client. They can't limit what another program running as the same user sends to the bridge directly. This includes an assistant that can use a shell (terminal). Don't let an assistant that you don't trust use a shell on the computer where Flame runs.

### Computers shared by several users

`flame_mcp_bridge.py` is installed in `/opt/Autodesk/shared/python/`, and every user and every Flame version on the computer loads it. If other users can write to this folder, they can replace `flame_mcp_bridge.py`. Check the folder's permissions. `flame_mcp_bridge.py` doesn't load the main part of the bridge, `impl.py`, if other users can change `impl.py` or its folder.

### Files that flame-mcp keeps

flame-mcp creates its files on disk so that only the user who started Flame can read them (files 0600, folders 0700). It doesn't change the permissions of folders that already exist.

| What | Where | How long |
| --- | --- | --- |
| Records of the changes that the assistant made | `~/Library/Application Support/flame-mcp/journal/` | 30 days |
| Batch setup files saved for undo | `journal/checkpoints/` | Not deleted automatically |
| Copies saved for undo, made before the assistant deletes or edits a clip or sequence | The `flame_mcp_checkpoints` library in each project | Until you delete them with `journal.purge_checkpoints` |
| Operation results too large to return to the assistant directly (over about 60 KB; the assistant receives only the location of the file) | `~/Library/Application Support/flame-mcp/spill/` | 7 days (when the server starts, it deletes files older than 7 days) |
| Bridge logs | `~/Library/Application Support/flame-mcp/logs/` | Up to 4 files (when a file reaches 2 MB, the bridge starts a new file and deletes the oldest one) |

## Undo

To undo a change, ask the assistant ("Undo the last change"). The assistant runs `journal.revert`, which uses the change record to put things back. If a change can't be undone one value at a time, `journal.revert` uses the setup file or the copy saved for undo. If the target changed after the assistant's change, `journal.revert` stops and doesn't overwrite it.

Changes made in one `plan.run` are undone together.

> [!WARNING]
> **After the assistant changes something, don't press Ctrl+Z.** Flame's undo history doesn't always record changes such as renames and attribute changes. If you press Ctrl+Z, Flame might undo an earlier, unrelated action instead.

The following can't be undone. The description of each operation in [docs/ops.md](docs/ops.md) says whether it can be undone.

- Files written by a render, and the clips that the render created
- Saved setup files
- Shelf reels and Batch iterations that were created
- Copies saved for undo that were deleted with `journal.purge_checkpoints`
- Changes that the setup file saved for undo doesn't cover (for example, another Batch group or the Desktop)

When a change is undone from a setup file saved for undo, flame-mcp can't detect attribute values or node positions that you changed by hand afterward. Undoing can also remove those manual changes.

For details, see [docs/safety.md](docs/safety.md).

## Configuration

Usually, you don't need to configure anything. To change a setting, pass an environment variable to the server in your MCP client's settings.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FLAME_MCP_READONLY` | Off | `1`: use only operations that don't change or write anything |
| `FLAME_MCP_ALLOW_CATEGORIES` | All | The categories that the assistant can use, separated by commas (for example, `library,desktop,timeline,segment,journal`) |
| `FLAME_MCP_ALLOW_EFFECTS` | All | The operation effects that the assistant can use, separated by commas (`read`, `reversible`, `create`, `destructive`). An effect is what an operation does to the project, not a visual effect. |
| `FLAME_MCP_ENABLE_EVAL` | Off | `1`: turn on `eval.run` |
| `FLAME_MCP_CHECKPOINT_LIBRARY` | `flame_mcp_checkpoints` | The library where copies saved for undo are kept |

- Include `read` in `FLAME_MCP_ALLOW_EFFECTS`. Without it, every read is refused too.
- Without `journal` in `FLAME_MCP_ALLOW_CATEGORIES`, the assistant can't check its changes or undo them.
- If a setting isn't valid (for example, `FLAME_MCP_READONLY=true`, or a list that has only commas), the server doesn't start, and it shows the reason.

### Read-only mode

Use read-only mode to review or audit a project without changing anything.

```json
{
  "mcpServers": {
    "flame": {
      "command": "/path/to/flame-mcp/scripts/flame_mcp_server.sh",
      "env": { "FLAME_MCP_READONLY": "1" }
    }
  }
}
```

Only operations that change nothing are listed, and only they can run. `eval.run` is refused. To read a closed library by name, the server opens the library, reads it, and closes it again.

### Running arbitrary Python code (`eval.run`)

`eval.run` is the last resort, for tasks that no operation can do. It's available only when `FLAME_MCP_ENABLE_EVAL=1` is set, and it's never available in read-only mode.

> [!CAUTION]
> **`eval.run` runs arbitrary Python code inside Flame, with the permissions of the user who started Flame.** The code can read, write, and delete files, use the network, and start processes. A static check stops common mistakes, but it isn't a sandbox or a security boundary, and deliberately written code can get past it. A timeout only stops waiting for the code to finish. It doesn't stop the code. Code that never finishes can leave Flame not responding. Turn on `eval.run` only when you trust both the assistant and the project.

## Troubleshooting

### The assistant can't connect to Flame (`BRIDGE_UNAVAILABLE`)

1. Start Flame and open a project. The bridge accepts connections after a project is open.
2. In Flame's main menu, choose **MCP Bridge > Show status** to check that the bridge is running. If it shows `Bridge is not running.`, choose **Show last error** to see why.
3. If the main menu has no **MCP Bridge**, Flame didn't load the bridge. Quit Flame, run `install_bridge.sh`, and start Flame again. To check the installation, run `scripts/doctor.sh`.

While Batch is rendering, the assistant might not be able to connect until the render finishes.

### Multiple instances of Flame are running (`BRIDGE_AMBIGUOUS`)

If the bridge is running in more than one instance of Flame, flame-mcp doesn't connect to any of them. This keeps it from changing the wrong instance. Quit the instances of Flame that you aren't using.

### An operation stopped responding (`TIMEOUT`, `BRIDGE_LOST`)

When this error appears, flame-mcp can't tell whether the change took effect. The assistant doesn't make another change until it has checked the actual state.

`BRIDGE_LOST` means that Flame might have crashed. If Flame has quit, start it again, and then ask the assistant to check the state.

### A change had no effect (`SILENT_NOOP`)

Check whether a dialog is open in Flame. While a dialog is open, some changes to the Media Panel might not take effect. Close the dialog, and then ask the assistant again.

### The flame tools don't appear in the client

- Quit the client completely, including any process left in the menu bar or the system tray, and start it again.
- Check the path of `flame_mcp_server.sh` in the client's settings.
- If an environment variable has a value that isn't valid, the server doesn't start. Check [Configuration](#configuration).

### A call is refused with `non-ASCII character`

flame-mcp can send only ASCII characters to Flame. You can still talk to the assistant in any language. Use ASCII characters in the names of clips and nodes, and in file paths.

### The server doesn't start and shows `non-ASCII character` for `the journal directory`

The path of the folder where flame-mcp keeps its change records contains a character that isn't ASCII. flame-mcp puts this path in the code that it sends to Flame, so the path must be ASCII. Set the environment variable `FLAME_MCP_JOURNAL_DIR` to a folder path that uses only ASCII characters. For how to pass an environment variable, see [Configuration](#configuration).

### The assistant says it can't do something

- If you limited the operations in [Configuration](#configuration), the assistant doesn't see the operations that you excluded.
- The assistant might answer without looking for an operation. Ask it to search with `flame_catalog`.
- If no operation fits, flame-mcp can't do that task. For the list of operations, see [docs/ops.md](docs/ops.md).

### If the problem continues

Report it in GitHub Issues. The following information helps find the cause:

- Your Flame and macOS versions
- The output of `scripts/doctor.sh`
- What **MCP Bridge > Show last error** shows
- The relevant part of `~/Library/Application Support/flame-mcp/logs/bridge.log` (it contains file paths and project names, so paste only the parts that you're comfortable making public)

Report security vulnerabilities as described in [SECURITY.md](.github/SECURITY.md), not in Issues.

## Limitations

- Not supported: transitions, markers, audio, connected segments, deleting reels or folders, and Linux.
- You can't delete anything in a library, or move anything out of a library. This keeps past work stored in libraries from being changed by mistake. To take something out of a library, use `desktop.copy` (the original stays in the library).
- A sequence or clip that's restored by an undo goes to the end of its reel. Flame's API has no way to set the order of items in a reel. To undo a change to a sequence, flame-mcp replaces the sequence with the copy that it saved before the change. The changed sequence isn't deleted. It moves to the library for undo copies (`flame_mcp_checkpoints`).
- The `flame_context` tool lists known pitfalls in its `pitfalls` field.

## How it works

```
MCP client  <-- stdio -->  flame-mcp server  <-- Unix socket -->  bridge inside Flame  -->  Flame Python API
```

The server gives the assistant four tools. The assistant calls these tools itself, and you don't use them directly. Most work uses two of them: the assistant finds an operation with `flame_catalog` and runs it with `flame_do`.

| Tool | What the assistant uses it for | Changes the project |
| --- | --- | --- |
| `flame_catalog` | Find the operations that it can use, and read their descriptions | No |
| `flame_do` | Run one operation | Depends on the operation |
| `flame_context` | Learn the working rules and Flame's current state | No |
| `flame_version_info` | Check the connection and the settings | No |

For details, see [docs/architecture.md](docs/architecture.md).

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ops.md](docs/ops.md) | Every operation, with its effect and whether it can be undone (generated) |
| [docs/architecture.md](docs/architecture.md) | How the server, the bridge, and the change records work |
| [docs/safety.md](docs/safety.md) | What is undone and how, what can't be undone, and how to read each error |
| [docs/testing.md](docs/testing.md) | How to run the tests, and what each regression test on a real Flame checks |

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The unit tests in `tests/` run without Flame. The regression tests on a real Flame are in `scripts/regress.sh`. Because they change Flame, they run only in a dedicated test project. For setup and instructions, see [docs/testing.md](docs/testing.md).

## Questions, requests, and bug reports

Ask questions, make requests, and report bugs in GitHub Issues. To propose a code change, discuss it in an issue before you open a pull request.

## Acknowledgments

- flame-mcp adapts the design of [mcp-aftereffects](https://github.com/kumoproductions/mcp-aftereffects) by kumo.productions (MIT), including the split between a tool that finds operations and a tool that runs them. The operation registry, argument validation, and policy are ported from it. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- [abrahamADSK/flame-mcp](https://github.com/abrahamADSK/flame-mcp) and [dgirondi/FlameMCP](https://github.com/dgirondi/FlameMCP) were references during research.
- flame-mcp was developed with [Claude Code](https://claude.com/claude-code). The author made the design decisions, reviewed the code, and tested it in Flame.

## Trademarks

Autodesk and Flame are registered trademarks or trademarks of Autodesk, Inc. This project is an independent, unofficial tool. It isn't affiliated with Autodesk, Inc., and it isn't endorsed or supported by Autodesk.

## License

MIT. See [LICENSE](LICENSE).
