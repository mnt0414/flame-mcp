# Architecture

This page explains how flame-mcp is built: which processes take part, where each piece of code runs, and why it is built that way. It is written for people who review, maintain, or consider adopting the server.

* For the list of operations, see [ops.md](ops.md).
* For the promises the server makes, how writes are undone, and what cannot be undone, see [safety.md](safety.md).

Statements about Flame's behavior come from tests on Flame 2025.2.4 on macOS, and each one says what was tested. The end-to-end tests also passed on Flame 2026.2.3, but the individual measurements were not repeated there unless a statement says so. Statements about the server come from its code.

## Overview

flame-mcp has four layers. Only the bridge runs inside Flame.

```text
MCP client (for example Claude)
    |  stdio, JSON-RPC
    v
MCP server        python -m flame_mcp        runs outside Flame
    |  Unix domain socket, one JSON request per connection
    v
Bridge            stub + body                runs inside Flame
    |  a timer on Flame's main thread calls flame.schedule_idle_event
    v
Flame Python API  the flame module           called on the main thread only
```

* The **MCP server** offers four tools, checks every call, writes the journal, builds the Python code to run in Flame, and assembles the response.
* The **bridge** receives code from the socket and runs it on Flame's main thread. It holds no knowledge of any operation.
* The **Flame Python API** is the only way flame-mcp reads or changes the project.

Three decisions shape the rest of this page:

* **A closed set of operations.** The AI assistant picks from named operations. It does not write Python for Flame. The one exception, `eval.run`, is a last resort. It is off unless you turn it on, and the server checks every call to it (see [Policy](#policy)).
* **Every Flame call runs on Flame's main thread.** See [Threads](#threads).
* **Record before writing, read back after.** The server writes its intent to a journal before it writes to Flame, and it reads the result back afterward. See [Life of a call](#life-of-a-call) and [Journal and checkpoints](#journal-and-checkpoints).

### Where files live

Apart from the stub and Flame's own logs, everything is under `~/Library/Application Support/flame-mcp/`.

| Path | Content |
| --- | --- |
| `/opt/Autodesk/shared/python/flame_mcp_bridge.py` | The stub. |
| `bridge/impl.py` | The bridge body. |
| `run/flame-mcp-VERSION.sock` | The bridge socket. VERSION is the Flame version. The directory is mode 0700 and the socket is mode 0600. |
| `run/inflight/` | One record for each bridge call that has not been answered. Files are mode 0600 and the directory is mode 0700. The server removes records older than 7 days when it starts. |
| `run/results/` | Results of calls that outlived their wait, kept until fetched. The directory is mode 0700. |
| `journal/journal_YYYYMMDD.jsonl` | The write journal. Files are mode 0600 and the directory is mode 0700. The server removes files untouched for 30 days when it starts. |
| `journal/checkpoints/` | Batch setup files saved as checkpoints. Flame creates the directory with mode 0700 and the `.batch` files with mode 0600. They stay until you delete them. |
| `logs/bridge.log` | The bridge's own log, with its rotated copies. The files are mode 0600 and the `logs` directory is mode 0700. |
| `spill/` | Responses that were too large to return. Files are mode 0600 and the directory is mode 0700. The server removes spill files older than 7 days when it starts. |

The environment variables `FLAME_MCP_RUN_DIR`, `FLAME_MCP_JOURNAL_DIR`, `FLAME_MCP_SPILL_DIR`, and `FLAME_MCP_BRIDGE_DIR` move the `run`, `journal`, `spill`, and `bridge` directories. `FLAME_MCP_BRIDGE_DIR` is for development. The server does not put its own files in `/tmp`.

The modes above apply to files and directories that flame-mcp creates. It does not change the mode of an existing journal, spill, `logs` or checkpoint directory. [safety.md](safety.md#who-can-read-the-files) lists the modes, and [safety.md](safety.md#what-the-server-keeps-and-for-how-long) lists how long each kind of file stays.

## Tools and operations

The server exposes a small, fixed set of tools. The operations behind them live in a registry.

### Four tools

| Tool | Purpose |
| --- | --- |
| `flame_version_info` | Server and bridge identity, policy summary, operation counts, unanswered calls, and journal state. It sends only a `hello` to the bridge and touches no project state. |
| `flame_catalog` | Lists operations, one category, or the full description of one operation. |
| `flame_do` | Runs one operation and returns a response envelope. |
| `flame_context` | Shared conventions (undo contract, thread contract, selector vocabulary, limits, known pitfalls) plus a live snapshot (library names, current tab, selection count). |

The tool list is fixed at four. An MCP client loads every tool definition into its context, so new capabilities arrive as operations in a registry, not as new tools. An assistant finds an operation with `flame_catalog`, reads its description, and runs it with `flame_do`.

The tools carry MCP annotations. `flame_version_info`, `flame_catalog` and `flame_context` have `readOnlyHint=true`. `flame_do` has `readOnlyHint=false` and `destructiveHint=true`, because it runs whatever operation it is given. The server also sends short instructions to the client. One of them tells the assistant to ask the user to confirm before an operation that cannot be undone or that overwrites something. Annotations and instructions are hints, and the client decides what to do with them.

### The registry

Each operation is declared once as an `OperationSpec` and registered in the `Registry`. The registry is the single source of truth for the catalog, for `flame_do`, and for [ops.md](ops.md), which is generated from it.

A declaration holds:

* The name, which starts with its category (`segment.trim` belongs to `segment`), and the description.
* The parameters, with types, defaults, and limits.
* The selector kinds the operation accepts.
* `effect` and `undo`, described below.
* The execution mode (`thread`).
* `to_python`, a function that returns the code to run in Flame, or `server_handler` for an operation that never touches Flame.
* An optional `verify` function that returns code to read the result back.
* `requires`, the Flame API names the operation uses.

The registry rejects a bad declaration when the server starts, so a mistake cannot wait for a live call:

* An operation that runs in Flame must have `to_python`, and an operation that runs on the server must have `server_handler`.
* The code from `to_python` is generated from sample arguments and parsed. For each selector kind the operation accepts, it is generated and parsed again.
* The generated code of an operation that writes may not contain a loop or comprehension. Repeated writes are separate calls, so each callback on Flame's main thread stays short.
* Each name in `requires` must exist in the bundled symbol table, which was captured from Flame 2025.2.4.

### Categories

A category groups operations that share a Flame area, such as `segment`, `batch`, `timeline`, or `journal`. A category sets the default `thread` for its operations and the set of threads they may use. A category is also the unit of policy (see below). An operation that declares a mode its category does not allow fails registration.

The effective `thread` and where it came from (operation, category, or global default) appear in the `execution` field of every response and in the detailed `flame_catalog` entry.

### Effect and undo

Every operation declares an `effect` (what a call does to the project) and an `undo` class (how `journal.revert` reverses it). [safety.md](safety.md#effect-and-undo-classes) defines the values, and [ops.md](ops.md) shows both for each operation.

If an operation does not declare `undo`, the registry derives it from `effect`: `read` gives `none`, `reversible` gives `value`, and `create` and `destructive` give `checkpoint`. Many operations override the default. The server never uses Flame's own Undo, so `undoSteps` is 0 for writes.

### Policy

A policy limits what a server instance may do. The server reads it from environment variables when it starts. [safety.md](safety.md#policy) lists the variables and what each one does. An unusable value, such as `FLAME_MCP_READONLY=true`, stops the server at start-up with a message on stderr, before any MCP traffic.

The same policy object is applied in three places, so they cannot disagree:

1. The `op` parameter of `flame_do` lists only the operations the policy allows.
2. `flame_catalog` lists only those operations.
3. `flame_do` checks the policy again when it runs a call, and returns `POLICY_DENIED` with a hint.

`flame_version_info` reports the policy in one line. Its `eval` part is `eval=disabled`, `eval=ENABLED` or `eval=disabled (readonly)`.

`eval.run` runs any Python, so the policy treats it apart from the other operations. It is off unless `FLAME_MCP_ENABLE_EVAL=1` is set and `FLAME_MCP_READONLY` is not. Only then is it listed in `flame_catalog` and in the `op` list of `flame_do`, and only then can a call run. Otherwise the server refuses every call with `POLICY_DENIED`, including a step of `plan.run`, and the `hint` names the switch. When it is on, the operation declares `effect` `read`, but each call also declares an effect of its own, and a call can write. The registry-level effect filter cannot see that, so the server checks the declared effect of each `eval.run` call against `FLAME_MCP_ALLOW_EFFECTS`. [safety.md](safety.md#free-form-python-evalrun) describes the rules and their limits.

A call to some other operations also counts as `destructive` for `FLAME_MCP_ALLOW_EFFECTS`, although the operation declares a lower effect. An overwrite flag makes the call destructive: `overwrite=true` on `batch.save_setup`, `node.save_setup`, `segment.effect_save_setup` and `batch.iterate`, and `allow_overwrite=true` on `render.batch`. Each of these operations does this in its `precheck`, so the check runs for `flame_do` and for every step of `plan.run` before anything is sent. [safety.md](safety.md#calls-that-count-as-destructive) lists them.

## The bridge

The bridge is the only code that runs inside Flame. It is split into a small stub and a body, and it finds its way to the server through a socket.

### Stub and body

The bridge has two files:

* The **stub**, `/opt/Autodesk/shared/python/flame_mcp_bridge.py`, is the only file placed under `/opt/Autodesk`. Flame loads it as a Python hook. It is copied there once, as a real file and not a symlink, and rarely changes.
* The **body**, `~/Library/Application Support/flame-mcp/bridge/impl.py`, holds the socket server and the relay to the main thread. The stub loads it.

Splitting the bridge this way has these effects:

* **The body can change without touching `/opt/Autodesk`.** Deploying a new body is a file copy into the user's own directory.
* **The body can be reloaded without restarting Flame.** The stub adds an **MCP Bridge** submenu to Flame's main menu with **Reload bridge**, **Show status**, and **Show last error**. A reload reads `impl.py` from disk, compiles it, and runs it again, so it always runs the current source. It stops the old bridge, starts the new one, and, if the new one fails to start, starts the old one again. Flame's own **Reload Python Hooks** does not reload the body, because Flame knows only the stub.
* **A broken body leaves the menu working.** The stub catches any exception from loading or starting the body and keeps the message. The menu stays registered, so you can read the error and reload after a fix.
* **A body that someone else can change is not loaded.** Before the stub reads `impl.py`, it checks that neither `impl.py` nor its directory is a symlink, that the user who runs Flame owns both, and that neither is writable by group or other (`mode & 0o022` is 0). It opens `impl.py` with `O_NOFOLLOW` and checks the open file with `fstat`. If a check fails, the stub does not load the file, and **Show last error** gives the reason. A reload makes the same checks.
* **Older Flame installs can still parse the stub.** Older Flame versions such as 2023.3.2 run Python 3.9 and read the same `/opt/Autodesk/shared/python` directory. The stub uses no syntax newer than Python 3.9.

### Installing and removing

`scripts/install_bridge.sh` copies the stub and the body as real files:

* It writes each file to a temporary file next to its target, compares it with the source, and then replaces the target with `mv`. Flame never reads a half-written file.
* The stub gets mode 0644 and the body gets mode 0600. The script removes group and other write permission from the body directory.
* It backs up a file it replaces, and it refuses to touch a target that is a symlink.
* It refuses to run as root, for example with `sudo`, because the loader refuses a body that another user owns. `--dry-run` is allowed as root.
* It refuses to run while Flame is running. With `--dry-run`, it warns, goes on and writes nothing.

`scripts/uninstall_bridge.sh [--dry-run]` removes what the installer put in place: the stub, the stub's `.pyc` files, the body's `impl.py` and `__pycache__`, and the body directory if nothing else is left in it. It backs up the stub and the body first. It does not remove a symlink. It reports it and exits with status 1. It does not remove data such as the journal, the checkpoints and the logs. It prints where they are. It refuses to run while Flame is running, except with `--dry-run`.

### ABI and version checks

Two checks keep a stub, a body, and a server from running together by mistake:

* **ABI.** The body defines `BRIDGE_ABI`. The stub refuses to start a body whose ABI differs from the one it expects. The server also compares the `bridgeAbi` in the bridge's `hello` reply with its own copy and treats a mismatch as no usable bridge (`BRIDGE_UNAVAILABLE`, with the mismatch in the message).
* **Flame version.** The server and the bridge were tested only on Flame 2025.2.4 and Flame 2026.2.3 (the end-to-end tests passed on both). The bridge is written to start on any other version and to warn in `bridge.log` and in the **Show status** dialog. `flame_version_info` reports `untestedFlameVersion` and adds a notice.

### Starting and stopping

The stub implements two lifecycle hooks, `app_initialized` and `app_exited`, and one menu hook, `get_main_menu_custom_ui_actions`. `app_initialized` starts the bridge. Flame's hook file describes this hook as called after a project is loaded, so before a project is open there is no socket. When the hook runs again in the same session (the project changed), the stub only updates the project name that `hello` reports. `app_exited` stops the bridge and removes the socket.

Starting the bridge:

1. Create the `run` directory with mode 0700.
2. Probe the socket path by connecting to it. If something answers, another bridge is alive: refuse to start and log the reason. If nothing answers, delete the stale file.
3. Bind the socket (mode 0600) and start the accept thread.
4. Start the relay timer on the main thread (see [Threads](#threads)).

The socket file name includes the Flame version, for example `flame-mcp-2025.2.4.sock`. The bridge refuses a socket path longer than 103 bytes.

### Finding the bridge

The server does not trust that a socket file means a live bridge. For each `flame-mcp-*.sock` in the `run` directory, it connects and sends `hello`, waiting up to 1 second. A dead socket file refuses the connection and is skipped. The `hello` reply carries the ABI, Flame version, process ID, project name, and optional features.

* No live bridge: `BRIDGE_UNAVAILABLE`. Wait for Flame to start and open a project.
* Exactly one live bridge: use it.
* More than one live bridge, for example two Flame versions running at once: `BRIDGE_AMBIGUOUS`. The server never picks one for you, because a write to the wrong Flame is hard to recover from. Set `FLAME_BRIDGE_SOCKET` to the socket you want.

`BRIDGE_UNAVAILABLE` and `BRIDGE_LOST` mean opposite things. The first means the bridge is not there yet, so waiting is reasonable. The second means the connection broke during a call, which suggests Flame crashed, so you check what happened before doing anything else.

### Wire protocol

A request is one line of JSON that ends with a newline, and the server opens a new connection for each request. The `kind` is `hello` (identity and capabilities), `ping` (liveness), `exec` (run code on the main thread, with a `thread` mode and a timeout), or `poll` (fetch the result of an earlier call by its handle). The bridge refuses lines longer than 8 MiB. It checks the request and compiles the code before it queues anything, so a syntax error returns at once as `FLAME_API_ERROR`.

The bridge limits what a client can do to it:

* **Connections.** It accepts at most 32 connections at a time (`MAX_CONNECTIONS`). It closes a connection beyond that at once.
* **Idle time.** It closes a connection that sends nothing for 300 seconds (`CONNECTION_IDLE_TIMEOUT_S`).
* **Timeout value.** A `timeoutS` that is not a finite number returns `protocolError`.
* **Unexpected errors.** If a handler raises an exception that the bridge did not expect, the bridge logs it and returns `protocolError`. The handler does not die without an answer.
* **Home directory.** A traceback and an error message that the bridge returns show `~` in place of the user's home directory.

## Threads

Every Flame API call runs on Flame's main thread. This section gives the measurements behind that rule, then shows how a request reaches the main thread.

### The rule

The bridge runs socket handler threads, but those threads never call the `flame` module. Every Flame API call runs on Flame's main thread. This includes reads, and it includes `flame.schedule_idle_event` itself.

### Crashes from worker threads

On Flame 2025.2.4, the same schedule of calls ran from a worker thread and from the main thread. The table shows how many runs crashed Flame.

| Call | From a worker thread | From the main thread |
| --- | --- | --- |
| Create reels in quick succession | 3 of 3 runs (two segmentation faults and one bus error). | 0 of 2 runs (25 and 163 reels). |
| Edit segments (`trim_head`, `slip`, `change_start_frame`) | 2 of 3 runs. One crash came in the series with 2 seconds between edits. | 0 of 2 runs, each with 100 edits. |
| Assign an object's `name` | 0 of 2 runs. | Not run. |
| Read values (five kinds of read) | 0 of 3 runs. One `selected_entries` call raised a `RuntimeError`. | Not run for the five reads. |

In the crashes, the crashed thread was Flame's main thread, in drawing code unrelated to the call, while the worker's own call returned normally.

Because the crash lands in code the call never touches, the server does not sort calls into safe and unsafe ones by what they do. It runs every call on the main thread, including the reads and the name assignment that did not crash in these runs. Running a call there costs a short delay.

### Freezes from `schedule_idle_event`

An earlier design had handler threads call `flame.schedule_idle_event` directly. On Flame 2025.2.4, Flame froze in an end-to-end test, and again in a stress test with four threads after 1,482 requests. The stack samples of both freezes had the same shape: the main thread waited for the Python global interpreter lock inside Flame's idle-event queue, and a handler thread waited on a lock inside `schedule_idle_event`. That is the shape of two threads that each wait for a lock the other holds.

The present design has handler threads only put requests on a Python queue. A timer on the main thread takes requests from the queue and calls `schedule_idle_event` itself. With this design, two stress runs of 2,000 requests each (four threads, 500 requests per thread) completed without a freeze.

### How a request reaches the main thread

```text
handler thread                          main thread
--------------                          -----------
receive and compile code
put request on the relay queue  ---->   relay timer tick (every 10 ms)
wait for the result                       take the requests queued so far
                                          schedule_idle_event(callback)
                                        Flame runs the callback when idle
                                          exec the code, catch every exception,
                                          store the result
send the reply                  <----   wake the handler
```

* The timer tick takes only the requests already queued when it starts, so one tick stays short.
* The callback catches its own exceptions and hands the result to the waiting handler. No exception escapes into Flame.
* The bridge does not withdraw a callback from Flame's queue. Instead, each request has a state (queued, running, done, or cancelled), and the callback checks it first. If the handler already gave up and marked the request cancelled, the callback returns without running anything. This is how a `queued` timeout can promise that the code did not run.
* Flame's own documentation for `schedule_idle_event` asks for short callbacks, because they run on the main application thread. Operations are built to match. A journaled write is several short calls (read, then write), a checkpointed write is several (checkpoint, run, fingerprint), and `plan.run` sends one call for each step.

If the relay timer fails to start, the socket still comes up, but every `exec` returns `MAIN_THREAD_UNAVAILABLE` until the bridge is reloaded.

### Execution modes

Each operation declares one of three modes.

| Mode | Runs | Used for |
| --- | --- | --- |
| `main` | On the main thread. The caller waits for the result. | Operations that call the Flame API. This is the default. |
| `main-async` | On the main thread. The caller waits for a limited time and then receives a handle. | Long operations. Only `render.batch` declares it. |
| `server` | On the MCP server, never in Flame. | Operations that do not touch Flame, such as `journal.*` and `plan.run`. A server operation reaches Flame only by calling other operations through the same path as `flame_do`. |

A foreground render keeps Flame busy. While it runs, other calls can fail with `BRIDGE_UNAVAILABLE` or `TIMEOUT`, so wait for a render through its handle and do not retry.

### Timeouts and handles

| Mode | Default wait | When the wait ends first |
| --- | --- | --- |
| `main` | 10 seconds (some operations, such as checkpoints, wait longer) | See below. |
| `main-async` | 30 seconds (`render.batch` takes its own `timeout_s`) | The call succeeds with `pending.handle`. The result is saved under `run/results/` when the call ends. |

The bridge caps any wait at 600 seconds. The server waits 5 seconds longer than the bridge, so the bridge's answer arrives first.

A `main` call that runs out of time returns `TIMEOUT` with a `timeoutPhase`:

* `queued`: The main thread had not started the call. The bridge cancels it, and it is guaranteed not to run.
* `running`: The call is still running and may finish later or may already have applied. The response carries `pending.handle`.

[safety.md](safety.md#after-an-unclear-result) says what to do after a timeout. The bridge deletes a result file when it is fetched. At start-up it also deletes result files older than 24 hours.

If the bridge gives no reply at all, the server sends `hello` again. A live bridge gives `TIMEOUT` ("bridge alive but no reply"). A silent one gives `BRIDGE_LOST`.

## Life of a call

This is what happens when an assistant calls `flame_do`:

1. **Resolve and check.** The server looks up the operation (`UNKNOWN_OPERATION` suggests the closest name), applies the policy (`POLICY_DENIED`), and validates the arguments against the declared parameters (`INVALID_ARGS`). Validation includes the shape of every selector. It fills defaults and clamps `limit`. An operation's `precheck`, such as the scan of `eval.run` code or the check of an overwrite flag, runs here. All of this happens before anything reaches the bridge.
2. **Guard.** If an earlier journaled write ended without an answer, the server refuses every non-read operation (`POLICY_DENIED`) until `journal.check` has decided what happened. The state lives in the server process.
3. **Journal the intent.** For a journaled write, the server reads the current values, appends an `intent` record, and flushes it. For a write behind a checkpoint, it takes the checkpoint first. See [Journal and checkpoints](#journal-and-checkpoints).
4. **Build the code.** `to_python` returns the operation's own code. The server wraps it in a script (see below).
5. **Send and run.** The server writes an in-flight record, sends the script to the bridge, and the bridge runs it on the main thread.
6. **Check the result.** The script runs the optional `verify` code and reads the project state back. The server turns the reply into an envelope. For a journaled write, it appends an `outcome` record.
7. **Limit the size.** The envelope is trimmed if it exceeds the byte budget.

### The script sent to Flame

Every operation runs inside one generated script. In order, the script has:

1. **The prelude.** Helper functions that all operations share. They read values (`_v`, `_j`), define the `_OpError` exception, and resolve selectors (`_resolve_path`, `_resolve_segment`, and others). Selector resolution lives here and not in each operation's code, so an operation resolves a target with one line.
2. **The operation code.** It is embedded as a string and compiled with a file name of the form `<op:NAME>`, where NAME is the operation name. A traceback names the operation and its line. The code runs inside a `try` block. An `_OpError` carries a specific error code, and any other exception becomes `FLAME_API_ERROR`.
3. **The verify code**, if the operation declares it, and only if the operation raised nothing.
4. **The context rider.** The script reads the project name, workspace name, and the number of selected items, each in its own `try` block. A field that cannot be read is `null`. The rider reports a count and not a list, so its cost does not grow with the selection.
5. **The result.** One dictionary holding the value, counts, the error, the verify outcome, and the rider.

### Silent failures

Some Flame calls raise nothing and change nothing. For example, `cut` outside the sequence and `replace` with a shorter clip both do nothing without an error, and `trim_head` can return `False` even when it worked. So the server does not trust return values. An operation reads the state back, and the script reports whether it matches. If an operation raised no error but its check fails, the envelope has `ok: false` and `errorCode: SILENT_NOOP`. If part of a call took effect, `partial` and `result.applied` say how much.

### Error codes

Each error code comes from one stage of a call. [safety.md](safety.md#error-codes) gives the meaning of each code and what to do about it.

* **Before Flame, on the server:** `UNKNOWN_OPERATION`, `INVALID_ARGS`, `POLICY_DENIED`.
* **Resolving a selector in Flame:** `SELECTOR_EMPTY`, `SELECTOR_AMBIGUOUS`, `SELECTOR_TYPE`.
* **In or after the operation:** `FLAME_API_ERROR`, `SILENT_NOOP`, `CONFLICT`, `PRECONDITION_UNMET`.
* **Reaching or waiting for the bridge:** `BRIDGE_UNAVAILABLE`, `BRIDGE_AMBIGUOUS`, `BRIDGE_LOST`, `TIMEOUT`, `MAIN_THREAD_UNAVAILABLE`.

Each code comes with a `hint` and a `retryable` flag.

### The response envelope

Every tool returns one shape.

| Field | Meaning |
| --- | --- |
| `ok`, `result` | Success flag and the operation's value. A failed operation can still return a `result`, and a call can be `ok` and still carry `partial`. |
| `affected`, `partial` | How many targets changed. `partial` counts what applied and what failed when only some did. |
| `undoSteps` | Always 0 for writes, because the server does not use Flame's Undo. |
| `error`, `errorCode`, `hint`, `retryable`, `traceback` | The failure, in words and as a code. |
| `pending`, `timeoutPhase` | A handle for a call that outlived its wait, and which phase it was in. |
| `context` | The rider: project, workspace, and selection count. |
| `execution` | The operation, its effective `thread` and `pacing`, where each came from, and its `effect` and `undo`. |
| `journal` | The group and record ids of a write, plus a ready-to-call revert when one exists. |
| `logs`, `logsNote` | Flame's error and warning lines logged during the call. See [Diagnostics](#diagnostics). |
| `crash` | On `BRIDGE_LOST`, the evidence collected after the connection broke. |
| `degraded` | Every way the server cut the response down. It never cuts silently. |
| `notices` | Short warnings, such as an untested Flame version or an unavailable Media Panel. |
| `durationMs` | Time spent on the call. |

### Limiting response size

Two mechanisms keep a response small:

1. **Paging.** Operations that list things take `limit` and `offset` and return `total`, `shown`, and `truncated`. The server fills a default `limit` of 50 and clamps it to a maximum of 500 (an operation can declare its own values). A `limit` of 0 or less becomes 1, not "no limit". Each change appears in `degraded`.
2. **A byte budget of 60,000 bytes** for the whole envelope. If the envelope is larger, the server applies these steps in order and stops when it fits: cut the traceback and the logs to their last 20 lines, replace lists in `result` with a count, and finally write the envelope as it was before those cuts (full traceback, logs and `result`) to a file in the `spill` directory and return its path in `result.spilledTo`. Each step is recorded in `degraded`. The spill file is named `<32 lowercase hex characters>.json`. When the server starts, it removes spill files older than 7 days that have this form of name, and it leaves other files in the folder alone.

## Selectors

An operation points at its target with a structured selector, an object with a `kind`, and not with a name.

```json
{"kind": "segment",
 "parent": {"kind": "path", "path": ["Library", "Reel", "Sequence"]},
 "version": 0, "track": 0, "record_in": 86400}
```

Two reasons drive this design:

* **Objects do not last between calls.** In tests on Flame 2025.x, the objects obtained by iterating `reel.clips` were new Python wrappers on each access, so comparing them with `is` was always false. A selector is not an object. It is a recipe that the script resolves again on every call.
* **Names are not enough.** Names repeat (several nodes in a Batch group can share a name). The server addresses a segment by its position inside a sequence: version, track, and either index or record-in time.

The server checks the shape of a selector before the call, against the kinds the operation accepts. The script then resolves it. A selector that matches nothing returns `SELECTOR_EMPTY`. One that matches several returns `SELECTOR_AMBIGUOUS` and, where it can, names the candidates.

| Kind | Targets |
| --- | --- |
| `selection` | The current Media Panel selection. |
| `wiretap` | An object by wiretap ID, a stable ID from `get_wiretap_node_id()`. |
| `path` | A name path from a library, or with `root: "desktop"`, from the Desktop. |
| `segment` | A segment by version, track, and either `index` or `record_in`. Its `parent` is another selector for the sequence. An optional `expect_source_name` stops the call with `CONFLICT` if the segment found is not the one you expect. |
| `batch` | A Batch group on the Desktop, by `uid` or `name`. The group need not be current. |
| `node` | A node in a Batch group, by name. `pos` picks one of several nodes that share a name. |
| `action_node` | A node inside an Action, Image, or GMask Tracer node. |
| `desktop` | The current workspace's Desktop. |
| `timeline` | The sequence the Timeline shows when the call runs. |

`flame_context` publishes the same list as `selectorVocabulary`. The server builds both from one table.

## Journal and checkpoints

[safety.md](safety.md) explains what a user can rely on. This section covers how the pieces fit.

### The write-ahead journal

The journal is a record of what the server attempted. It lives in `journal/journal_YYYYMMDD.jsonl`, one file for each local day. Each line is a JSON record, and files are append-only.

* **Record types** include `intent` (written and flushed before a write), `outcome` (after the answer), `unresolved` (the answer never came), `pending` and `polled` (a call with a handle), `check` (what `journal.check` found), `checkpoint`, and `purge`.
* **Group.** One `flame_do` call, or one `plan.run`, is one group. A revert is a new group that names the group it reverses.
* **Durability.** Each line is flushed to the operating system but not forced to disk. A flushed line survives the death of the server process. A power loss can lose the last lines, which the design accepts. A reader skips any line that does not parse, including a torn last line.
* **Lifetime.** The server deletes journal files that have not changed for 30 days when it starts. It keeps a file that holds a timeline checkpoint whose stashed copy has not been purged.
* **Not evidence.** The journal says what was attempted, not what the project contains. `journal.check` and `journal.revert` read Flame again before they decide anything.

A write whose undo is `value` runs as two round trips under a write lock. The server reads the current values first, appends the intent, and then sends the write with the values it expects. The generated code compares each value right before writing and writes nothing if one differs (`CONFLICT`). This also detects a change that a person makes in the Flame UI between the read and the write.

If the write's answer never comes (`TIMEOUT`, `BRIDGE_LOST`, or `BRIDGE_UNAVAILABLE`), there is no outcome. The server refuses further non-read operations until `journal.check` has read Flame and decided whether the write landed. The server never retries a write on its own.

### Checkpoints

A write with undo `checkpoint` runs behind an automatic checkpoint of its target. The server takes the checkpoint, journals an intent with a fingerprint of the target, runs the operation, reads the fingerprint again, and journals the outcome. `journal.revert` restores the checkpoint, but only while the target still has the fingerprint the call left. A fingerprint is a hash of the target's structure. If something else changed the target since, the revert refuses and reports a conflict.

There are two kinds of checkpoint:

| Kind | Target | How it is saved |
| --- | --- | --- |
| Batch | A Batch group | A setup file in `journal/checkpoints/`, plus a record with the group's fingerprint. |
| Timeline | A sequence or clip on the Desktop | A copy in a stash reel (`timeline_YYMMDD`, the date of the checkpoint) of the library `flame_mcp_checkpoints`. `FLAME_MCP_CHECKPOINT_LIBRARY` sets another library name. |

Checkpoint files and stashed copies stay until a person removes them or `journal.purge_checkpoints` deletes stashed copies. Each kind has limits on what a restore brings back. The description of each operation states them, and [safety.md](safety.md#what-each-undo-class-restores) summarizes them.

## Diagnostics

Flame's Python errors are often silent, so the server collects evidence from outside Flame.

### The app.log window

Flame writes its own log, `app.log`, under `/opt/Autodesk/log/` (the file name contains the version and the host name). The server reads the log of the Flame version it is connected to. `FLAME_MCP_APP_LOG` and `FLAME_MCP_SHELL_LOG` set other paths. The server records the log's size and inode before each call. After the call, it reads only what was added, keeps lines tagged `[error]` or `[warn]`, and returns the last 50 in `logs`. If the log cannot be read or rotated during the call, `logsNote` says so. The server reads the file itself, so it stays readable after Flame crashes.

`app.log` holds messages from Flame's C++ code, with a timestamp, a source file and line, and a severity tag. A search of `app.log` and `shell.log` on Flame 2025.2.4 did not find output from Python `print()` calls, so the bridge writes its own messages to `bridge.log`. The server also turns one known line into a notice: when Flame logs that the Media Panel is unavailable (for example, a dialog is open), writes to the Media Panel can silently do nothing.

### bridge.log

`logs/bridge.log` is a rotating log (2 MB per file, three backups). It records bridge start and stop, refusals to start, relay errors, failed result-file writes, refused connections and the untested-version warning. The log files are mode 0600. At start-up, the bridge also sets an existing `bridge.log` and its backups to mode 0600.

### In-flight records and BRIDGE_LOST

Before the server sends a call, it writes `run/inflight/ID.json`, where ID identifies the call. The record holds the operation name, the mode, the time, the socket, and the code it sent. The server deletes the record when it receives an answer. It keeps the record when the connection is lost or the wait times out.

When the connection breaks, the server returns `BRIDGE_LOST` and waits 1 second, because Flame can write its last log lines a moment after the socket closes. It then fills the `crash` field with:

* The path of the in-flight record, so you can see what code was running.
* Lines containing `abnormal termination` that were added to `shell.log` during the call.
* The last 20 lines of `app.log`.

`journal.inflight` lists the records that remain. `flame_version_info` shows their count and the newest one. The server deletes records older than 7 days when it starts.
