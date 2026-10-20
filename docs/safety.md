# Safety

flame-mcp lets an AI assistant change a Flame project. This page describes the promises the server makes when it does, how to undo a change, what it cannot undo, and how to read a result that went wrong.

For the parts that make these promises work (bridge, journal, checkpoints), see [How it works](architecture.md). For every operation and its effect and undo class, see the [operation index](ops.md).

The Flame behavior described on this page was measured on macOS with Flame 2025.2.4. The end-to-end tests also passed on Flame 2026.2.3, but the individual measurements were not repeated there. Statements about the server's own rules, such as the policy and how long files are kept, come from its code. See [Tested range](#tested-range).

## The promises

1. **Read before write, never guess.** An operation reads its target before it writes, and it does not guess a name, an index or a value.
2. **Write, then read back.** After a write, the operation reads the target again and judges the result from what it reads. It does not trust the return value of a Flame call. Several Flame calls raise no error and change nothing, and some return `False` after they worked.
3. **Undo with `journal.revert`, and only with it.** Every write that can be undone returns a `journal` field with a group id. `journal.revert(group)` puts the change back.
4. **Stop on doubt.** If the state is not what a revert or a write expects, the server refuses and reports the current state. It does not overwrite someone else's change.
5. **A closed set of operations, with one switch-gated exception.** The assistant gets named operations. `eval.run`, which runs free-form Python, is off unless you set `FLAME_MCP_ENABLE_EVAL=1`. With `FLAME_MCP_READONLY=1`, the server refuses every operation that is not a plain read, and it refuses every `eval.run` call, so the server does not change the contents of the project. A read operation whose selector names a closed library opens that library and closes it again. See [Free-form Python](#free-form-python-evalrun).

A write passes through these steps. A step that fails ends the call before the next one starts.

```text
value writes (rename, set_attributes ...)

  read current value -> record intent -> write only if the value -> read back -> record outcome
                        (before writing)   is still the one read      and judge

checkpointed writes (segment.trim, node.create, batch.load_setup ...)

  save checkpoint -> record intent -> run -> read fingerprint -> record outcome
  (copy or file)     (before running)                            (journal.revert restores
                                                                  the checkpoint)
```

## Why you must not press Ctrl+Z

Do not undo with Ctrl+Z in Flame. Use `journal.revert`.

* Flame has one Undo stack, and it is shared with the person at the keyboard. Ctrl+Z removes the last thing on the stack, not the last thing the server did.
* Writes made with `PyAttribute.set_value` may not be put on that stack. `desktop.rename` and every `*.set_attributes` operation write this way. Two cases were measured on Flame 2025.2.4. After a rename of a reel on the Desktop, Ctrl+Z removed an earlier, unrelated operation (the one made just before the rename). The same happened after a write to a Batch node attribute.
* The journal does not know about a Ctrl+Z. After one, a later `journal.revert` can fail with `CONFLICT` or report `alreadyGone`.
* For these reasons, every writing operation reports `undoSteps` as 0, and `eval.run` refuses code that calls `execute_shortcut`, which can send Undo.

## Effect and undo classes

Every operation declares two labels. Both appear in `flame_catalog` and in [ops.md](ops.md), which lists the pair for each operation.

### Effect

The effect says what the operation does to the project. The read-only policy and the effect filter use it ([Policy](#policy)).

| effect | meaning |
| --- | --- |
| `read` | Changes nothing itself. |
| `reversible` | Changes something that already exists. |
| `create` | Creates something new: an object in the project, a file, or a render's output. |
| `destructive` | Deletes or replaces something that exists. |

`eval.run` and `plan.run` carry the effect `read`, but they can run writes. `eval.run` is available only when you turn it on ([Free-form Python](#free-form-python-evalrun)). Once it is on, it can write when the call declares `destructive`, and it can also write when a call that declares `read` gets its code past [the check before sending](#the-check-before-sending), which reads names and not behavior. `plan.run` writes through its steps, and each step goes through the normal policy.

#### Calls that count as destructive

An operation keeps the effect it declares. A call to some operations counts as `destructive` for `FLAME_MCP_ALLOW_EFFECTS` when it carries a flag that replaces something that exists:

| call | flag |
| --- | --- |
| `batch.save_setup`, `node.save_setup`, `segment.effect_save_setup` | `overwrite=true` |
| `batch.iterate` | `overwrite=true` |
| `render.batch` | `allow_overwrite=true` |

If `destructive` is not in `FLAME_MCP_ALLOW_EFFECTS`, the server refuses such a call with `POLICY_DENIED` before it runs anything. This holds for a step of `plan.run` too. The operation's `effect` in `flame_catalog` does not change.

### Undo

The undo class says how `journal.revert` undoes the operation.

| undo | what `journal.revert` does |
| --- | --- |
| `none` | Nothing in the project to put back. The operation changes nothing, or it writes only files or stash copies. |
| `value` | Puts the previous value back, deletes what the call made, or puts the thing back where it was. |
| `checkpoint` | Restores the checkpoint taken before the call. |
| `unsupported` | The operation cannot be undone. `journal.revert` does not reverse it. |

### What each undo class restores

**`value`.** There are three forms.

* A write of attribute values (`desktop.rename`, `*.set_attributes`) records the values before the write. `journal.revert` writes them back, but only for keys that still hold what the group wrote. If one key differs, nothing is written (`CONFLICT`).
* An operation that makes something (`library.create`, `desktop.create`, `desktop.copy`, `mediahub.import`, `desktop.duplicate`, `segment.copy_to_media_panel`, `segment.match`, `timeline.create_sequence`) records what it made. `journal.revert` deletes it, but only while it is still as made. A copy, an import, a duplicate or an extracted clip must have the same name, duration and content. A created library, reel or Batch group must still be empty. Otherwise the revert stops with `CONFLICT` and deletes nothing.
* `desktop.move` moves the things back to their old parents, but only if all of them are still in the destination and all of their old parents still exist. `desktop.delete` copies each clip or sequence into a stash first, and `journal.revert` copies it back. In both cases, the restored things get new uids and go to the end of their parent. `desktop.reformat` works the same way for a clip it changes in place: the clip is stashed first, and `journal.revert` restores the stashed copy. The fingerprint does not see a clip's format (width, height, bit depth, ratio, scan mode, frame rate, start frame), so `desktop.reformat` also records the format it left. `journal.revert` reads the clip's format first and stops with `CONFLICT`, restoring nothing, if it is not the recorded one, for example because the clip was reformatted again. `force=true` overrides this check, and the override is journaled. After a restore, the server reads the restored clip's format back and reports it (`result.restored[].format`). With `duplicate=true`, `journal.revert` deletes the copy instead, under the same two rules.

**How `desktop.reformat` runs.** One Flame call reads every target, and every refusal is decided from that read, before anything is copied or written. In place, the clips that need a write are copied into the stash, ten to a call, and each copy is recorded as a checkpoint as soon as its call answers; if a copy does not have the format of its clip, the call stops before it writes anything. Then each clip gets an intent, one Flame call and an outcome of its own, all in one journal group. Flame first checks that the clip still has the fingerprint and the format that were read (else `CONFLICT` for that clip), then reformats, then changes the start frame if one was asked for, and the clip is judged by reading it back, not by Flame's answer. A Flame call that ends without an answer stops the call at that clip, and the server refuses writes until `journal.check` has read Flame again. `PyClip.reformat` gives the clip a new uid and wiretap id (measured on Flame 2025.2.4), so the Flame call reads the clip again after the write: `result.reformatted[].to` and the journal's `created` hold that new uid and selector, and the outcome records `replacedUid` -> `targetUid`, which `journal.revert` and `journal.check` follow. A write that ended without an answer has no outcome; `journal.check` then takes the one entry of the clip's reel that was not there before the call and has the clip's name for the reformatted clip, and does not guess (`diverged`) when there are two.

**`checkpoint` for a Batch group.** Before the write, the server saves the group's setup to a file (`batch.checkpoint`) and records the group's fingerprint. `journal.revert` loads that setup again, but only while the group's fingerprint is still the one the call left. The fingerprint covers the nodes, their wiring and the inner nodes of Action, Image and GMask Tracer nodes. It does not see attribute values or node positions.

**`checkpoint` for a sequence.** Before the write, the server copies the whole sequence into a stash reel in the checkpoint library ([Environment variables](#environment-variables)). `journal.revert` restores that copy, but only while the sequence's fingerprint is still the one the call left. The fingerprint covers versions, tracks, segments and the effects on segments.

A restored sequence is a copy. It has a new uid and a new wiretap id, and it sits at the end of its reel. The edited sequence is moved into the stash first, so a restore can itself be undone. The result gives the new selector in `result.restored.selector`.

**`unsupported`.** See [What cannot be undone](#what-cannot-be-undone).

**`none`.** The operation changes nothing in the project, or it writes only a file or a stash copy.

### Plans

`plan.run` runs several steps in one call. The whole plan is checked first, and if any step is invalid, nothing runs. All writes in a plan share one journal group, so one `journal.revert` puts them all back. If a plan holds writes to a sequence, it becomes one checkpointed write on that sequence: one copy and one restore for the whole plan.

Some operations cannot be a step of a plan, for example `render.batch`, `desktop.delete`, `desktop.reformat`, `mediahub.import`, `segment.create_connection`, `segment.remove_connection`, `segment.sync_connections` and the checkpointed Batch writes. The server refuses them before it runs anything. Call them on their own.

## What cannot be undone

`journal.revert` does not reverse the following. Each operation's description in `flame_catalog` says which of them apply.

* **Render output.** `render.batch` writes files to disk and puts clips into the shelf reel `Batch Renders`. They stay. The server has no operation to stop a render. By default, `render.batch` refuses to start when `render.plan` reports one or more `existingPlannedFiles` (files that a valid Write File node would replace). It returns `PRECONDITION_UNMET` with the problem `would_overwrite`. Pass `allow_overwrite=true` to render over those files. The same finding appears in `summary.wouldRefuse` of `render.plan`.
* **Setup files.** `batch.save_setup`, `node.save_setup`, `segment.effect_save_setup` and `batch.checkpoint` write files. Delete them by hand. The server never deletes them. Checkpoint files go to a `checkpoints` folder in the journal directory.
* **Shelf reels and iterations.** `batch.create_shelf_reel` and `batch.iterate` run behind a checkpoint, but shelf reels and iterations are outside a Batch setup. A restore does not restore them, so the server cannot revert their creation.
* **Imported media files.** The revert of `mediahub.import` calls `flame.delete` on the imported clips by uid, which removes the clips from the project. The server never deletes the files the import read. In tests on Flame 2025.2.4, one source file was still on disk after the clips imported from it were deleted.
* **Segment connections.** `segment.create_connection`, `segment.remove_connection` and `segment.sync_connections` leave no journal record, and a checkpoint covers only the sequence of the target, never connected segments in other sequences. The result's `inverse` is a presumed inverse call, not a revert. Replacing the media of a connected segment also replaces the media of the segments connected to it, in other sequences (measured on Flame 2025.2.4). `journal.revert` of a `segment.replace` restores the target's sequence only. `segment.replace` and `timeline.delete_segments` report the number of other connected segments (`connected_segments()` lists the segment itself too, measured, and that entry is left out) in `result.notices`, and `segment.connections` lists them. On a copy whose segments were connected to an original, `timeline.delete_segments` left the original sequence unchanged (measured).
* **Purged stash copies.** `journal.purge_checkpoints` deletes stashed copies. After that, a delete or edit whose stash was purged cannot be undone. A real run (`dry_run=false`) needs the `token` from a dry run. See [Journal files and stash copies](#journal-files-and-stash-copies).
* **Anything a checkpoint does not cover.** A destructive `eval.run` is checkpointed for its `target` Batch group only. Changes to another Batch group, the Desktop, a library, a clip's media or files on disk are not undone, and nothing stops them. A fingerprint also cannot see everything:
  * For a Batch group, it does not see attribute values or positions.
  * For a sequence, it does not see markers, transitions, audio, effect parameters that are not attributes, the names and attributes of segments, or the name of the sequence. These come back only as far as Flame's own copy brings them.
  * `timeline.overwrite`, `timeline.insert`, `timeline.cut`, `timeline.delete_segments`, `timeline.move_track` and `timeline.shorten` read the audio tracks of the sequence on their own, before and after, and report them in `result.audio`. A call that changed only the audio is not a `SILENT_NOOP`. The fingerprint itself still does not see audio, so `journal.changed` is false for such a call, and the revert check compares the video fingerprint only. The revert of `timeline.shorten` and of `timeline.move_track` did bring the audio rows back (measured on Flame 2025.2.4 and 2026.2.3); in the `timeline.move_track` measurements the audio did not change.
* **Unsaved work in a crashed Flame.** If Flame crashes, it can lose changes that were not saved. The journal records what the server attempted. It is not evidence of what the project contains, so every check and revert reads Flame again before it decides.

Restoring a Batch checkpoint also has side effects: `load_setup` drops empty schematic reels and adds Flame's default empty reels. The server corrects this when the checkpoint recorded the reels, and it reports the outcome in `result.reelsRestored` and `result.reelTidy`. Reels that hold clips are rebuilt with new uids, so address them by name after a restore.

## Protection of libraries

Libraries are where finished media lives, so the server never deletes or moves out their contents.

* `desktop.delete` refuses a clip or a sequence inside a library (`PRECONDITION_UNMET`). It also refuses reels, reel groups, folders, libraries and Batch groups. It deletes only clips and sequences on the Desktop.
* `desktop.move` refuses a source inside a library (`PRECONDITION_UNMET`). To take something out of a library, use `desktop.copy`. The original stays.
* `desktop.reformat` refuses a clip inside a library (`PRECONDITION_UNMET`). It also refuses sequences, reels and Batch groups.
* The revert of `desktop.copy`, `mediahub.import` and `desktop.duplicate` deletes the copy only while its name, duration and content are the ones recorded right after the call. If someone edited the copy, the revert stops with `CONFLICT` and deletes nothing.
* Two things are allowed to remove something in a library: the revert of this server's own write, and `journal.purge_checkpoints` in the checkpoint library. The revert of a `desktop.move` into a library moves things out again, because it undoes the server's own move.

A closed library reads as empty. On Flame 2025.2.4, a closed library reported no children, and reading its name did not open it. For this reason, an operation that names a library, by a wiretap id or by the first name of a path, opens the library first and closes it again afterwards. The result lists it in `librariesOpened`. If the library cannot be opened, or its state cannot be read, the call stops with `PRECONDITION_UNMET` before it does anything.

## ASCII only

Flame may crash on a 2-byte (non-ASCII) character, so the server sends Flame ASCII only. A name, a path, an `eval.run` code string, or any other string in the arguments, including a key of an object, that holds a non-ASCII character is refused with `INVALID_ARGS` before anything is sent. The error names the argument and the code point (for example `U+30C6`), and its advice is to use ASCII characters. The server also refuses to send code that is not ASCII (`INVALID_ARGS`, nothing sent). That message starts with `nothing was sent to Flame` and names the operation: `the code for '<operation>' holds a non-ASCII character U+XXXX`. The bridge refuses non-ASCII code again before it compiles it. The server does not start if its journal directory or `FLAME_MCP_CHECKPOINT_LIBRARY` holds a non-ASCII character, because both are put into the code it sends.

`eval.run` code must also not build a non-ASCII string at run time and hand it to Flame. The scan cannot see such a string, so the description of `eval.run` tells the assistant not to do it. The server does not refuse it.

## Reading a result

Every `flame_do` call returns the response envelope, whose fields [architecture.md](architecture.md#the-response-envelope) describes. When a call goes wrong, read these fields first: `errorCode`, `error` and `hint` say why it failed and what to do next, `retryable` says whether the same call can work later, `partial` and `result` say how far it got, `pending` and `timeoutPhase` describe a call that outlived its wait, `journal` holds the group to pass to `journal.revert`, and `crash` holds the evidence of a crash. A call can be `ok` and still carry `partial`.

### Error codes

| code | meaning | what to do |
| --- | --- | --- |
| `SELECTOR_EMPTY` | The selector matched nothing. | Check the selection or the path. Do not retry unchanged. |
| `SELECTOR_TYPE` | The target's type is not the type the operation expects. | Pick the right kind of target. |
| `SELECTOR_AMBIGUOUS` | The selector matched more than one target. | Narrow it: a node needs `pos`, a Batch group needs `uid`, a reel needs `reel_kind`. |
| `FLAME_API_ERROR` | Flame raised an exception, or what Flame did differs from the measured behavior. | Read `error` and `traceback`. After a write, check the target before you try again. |
| `SILENT_NOOP` | The call raised no error but had no effect you can observe. | Check the preconditions and read the target. If part of the call took effect, read `result.applied` or `partial`. For a Media Panel write, check that no modal is open in Flame. |
| `TIMEOUT` | The call did not finish in time. | Do not retry. See [After an unclear result](#after-an-unclear-result). |
| `MAIN_THREAD_UNAVAILABLE` | Flame runs, but its main thread is not ready. | Wait, then retry. |
| `BRIDGE_LOST` | The connection closed during the call. This suggests that Flame crashed. | Do not retry. Restart Flame and read its log, then run `journal.check`. |
| `BRIDGE_UNAVAILABLE` | No bridge is reachable. | Wait for Flame to start and a project to load, then retry. While a render runs, wait through its handle (`journal.check`) instead of retrying. |
| `BRIDGE_AMBIGUOUS` | More than one live bridge was found. | Set `FLAME_BRIDGE_SOCKET` to select one. |
| `PRECONDITION_UNMET` | A requirement of the operation is not met. | Read `error`, fix the cause (in Flame if needed), then retry. |
| `UNKNOWN_OPERATION` | No operation has that name. | Use `flame_catalog`. The error suggests the closest name. |
| `INVALID_ARGS` | The arguments failed validation. | Fix them. The error lists the issues. |
| `POLICY_DENIED` | The server's policy denies the operation, or the server refuses writes until `journal.check` runs. | Read `hint`. See [Policy](#policy). |
| `CONFLICT` | A value changed after it was read, so nothing was written. | Read the current state again, then decide. See [Conflicts](#conflicts-and-newergroups). |

`retryable` is `true` for `BRIDGE_UNAVAILABLE`, `MAIN_THREAD_UNAVAILABLE` and `CONFLICT`, and `false` for every other code. For `CONFLICT`, `true` means the call can succeed after you read the state again. It does not mean you should repeat it unchanged.

### After an unclear result

After `TIMEOUT` or `BRIDGE_LOST`, you do not know if a write landed. Follow these steps:

1. Do not run the call again. A repeated structural write can do the same change twice.
2. For a write, run `journal.check`. It reads Flame again and compares each key with the value before and the value the write was to set. It reports `landed`, `not_landed`, `diverged` (neither value, or only some keys landed), or `unknown` (Flame could not be read, so the intent stays open).
3. For a read that timed out with `pending.handle`, run `journal.poll` with that handle. It returns `pending` while the call still runs, and the result once. A handle can be taken once. Flame does not know a handle after a restart.
4. Read `timeoutPhase`. `queued` means the call never started. `running` means it can still finish, or can already have applied. [architecture.md](architecture.md#timeouts-and-handles) explains why.
5. After `BRIDGE_LOST`, restart Flame if needed and read its log before you write again.

While a write has no answer, the server refuses every later write with `POLICY_DENIED` and a hint that names the group. Reads still work. The refusal ends when `journal.check` has decided every such group. Any finding except `unknown` ends it, including `diverged`. The server keeps this state in its process only.

A render (`render.batch`) is checked by the same rule. If the wait ended with a pending handle, `journal.check` asks the bridge for the render's result and then counts the planned output files: `landed`, `partial` or `failed`.

### Partial results

`partial` and `result.applied` tell you how far a call got. Read them before you decide what to do next.

* `*.set_attributes`: Every key is checked first, and if one is rejected, nothing is written. Keys are then written in order. A write that raises no error but does not stick counts as failed (`SILENT_NOOP`), and the call stops there. `partial` is `{applied, failed}`, and `result` lists `before`, `after`, `applied` and `failed`.
* `plan.run` and `journal.revert`: `partial` is `{done, failed, notRun}`. Steps before the failure stay applied, and a plan reports `result.stoppedAt`. A revert that stops halfway leaves the group `partially_reverted`. Nothing is retried automatically.
* `desktop.delete` and `desktop.move`: If only some targets were processed, the call is `ok` with `partial` set to `{deleted, notDeleted}` or `{moved, notMoved}`. If none was, it is `SILENT_NOOP`.
* `desktop.reformat`: If only some clips were reformatted, the call is `ok` with `partial` set to `{reformatted, notReformatted}`. If none was, the error is the code the clips share (`SILENT_NOOP`, `FLAME_API_ERROR` or `CONFLICT`), otherwise `FLAME_API_ERROR`.

`journal.list` reports a group's state as one of `landed`, `not_landed`, `failed`, `partial`, `conflict`, `unknown`, `diverged` or `reverted`.

### Conflicts and `newerGroups`

`CONFLICT` has two sources, and in both nothing is written.

* **A write.** The value changed between the server's read and its write. `result.conflicts` gives `{key: {expected, current}}`. Read the value again before you decide.
* **A revert.** The target is no longer as the group left it, for example because someone edited it. The server will not overwrite that. `result` gives the expected and current values or fingerprints.

For a checkpointed group, a revert can fail because a later write changed the same target. In that case, `result.newerGroups` lists the later groups that landed and were not reverted, newest first, as `{group, op, ts}`. Revert them in that order, newest first, and then revert the original group. A restored sequence keeps its link to the older groups, so the list follows a sequence through its new uids.

`journal.revert` takes `force=true` for a checkpointed group. It restores even though the fingerprint differs, and the override is journaled. It also loses whatever was added to the group or sequence after the checkpoint. The server does not use it unless you pass it. A group of value writes has no `force`.

## Policy

The policy limits what the server will do. The server reads it from environment variables at startup and applies it in three places that always agree (see [Policy](architecture.md#policy) in the architecture page). A denied call returns `POLICY_DENIED` with a hint. `flame_version_info` shows the active policy in its `policy` field, for example `readonly=off, categories=all, effects=all, eval=disabled`. The last part is `eval=disabled`, `eval=ENABLED`, or `eval=disabled (readonly)`.

### Environment variables

| variable | default | effect |
| --- | --- | --- |
| `FLAME_MCP_READONLY` | unset | `1` allows only operations with effect `read`, and refuses every `eval.run` call. |
| `FLAME_MCP_ALLOW_CATEGORIES` | all | A comma-separated list of categories to allow, for example `library,desktop,timeline,segment,journal`. |
| `FLAME_MCP_ALLOW_EFFECTS` | all | A comma-separated list of effects to allow: `read`, `reversible`, `create`, `destructive`. |
| `FLAME_MCP_ENABLE_EVAL` | unset | `1` turns on `eval.run`, for a call that declares `read` and for one that declares `destructive`. Without it, `eval.run` is off. It has no effect when `FLAME_MCP_READONLY` is `1`. |
| `FLAME_MCP_CHECKPOINT_LIBRARY` | `flame_mcp_checkpoints` | The name of the library that holds the stash copies of sequences. |

A flag accepts only unset, an empty string, `0` or `1`. Any other value, for example `true`, makes the server print the reason and exit before it serves any request. An unknown effect in `FLAME_MCP_ALLOW_EFFECTS` does the same, and so does a list in `FLAME_MCP_ALLOW_EFFECTS` or `FLAME_MCP_ALLOW_CATEGORIES` that holds no names, such as `,` or `, ,`. A list that is unset, empty, or only spaces sets no limit. The journal directory is set by `FLAME_MCP_JOURNAL_DIR` (see [Where files live](architecture.md#where-files-live)).

Notes on the limits:

* `FLAME_MCP_READONLY=1` blocks every operation whose effect is not `read`, `journal.revert` included, and every `eval.run` call. A read-only server can read the journal but cannot revert.
* A limit on effects or categories applies to the journal operations too. `journal.revert` has the effect `reversible`, and `journal.purge_checkpoints` has the effect `destructive`. If you exclude the `journal` category, nobody can check or revert. Keep it in the list unless you want that.
* If you set `FLAME_MCP_ALLOW_EFFECTS`, include `read`. Without it, the server refuses every operation with effect `read`, including `plan.run`, the journal reads and, when it is on, `eval.run`.
* If you set `FLAME_MCP_ALLOW_EFFECTS` without `destructive`, the server also refuses the calls that [count as destructive](#calls-that-count-as-destructive).
* To stop Batch renders but allow other writes, leave the `render` category out of `FLAME_MCP_ALLOW_CATEGORIES`.

### Journal files and stash copies

* Journal files are described in [The write-ahead journal](architecture.md#the-write-ahead-journal), including how long the server keeps them.
* Stash copies stay in the checkpoint library, in a reel named `timeline_YYMMDD` for the day, until you run `journal.purge_checkpoints`. The default is a dry run that deletes nothing. A real run deletes only copies the journal recorded, only if older than `older_than_days`, and only if Flame confirms the copy is in the checkpoint library with the recorded uid and name. A copy that no record accounts for is listed under `unknown` and never deleted.
* A real run of `journal.purge_checkpoints` (`dry_run=false`) needs the `token` that a dry run returned in `result.token`. The token is a hash of the list of candidates. If you leave it out, or if the candidates changed since the dry run (a candidate was added or removed, or `older_than_days` differs), the call returns `PRECONDITION_UNMET` and deletes nothing. The token makes you look at the dry run first. It does not replace the approval of the person who owns the project.
* Batch checkpoint files stay on disk until you delete them.

### What the server keeps, and for how long

| What | Kept for |
| --- | --- |
| Journal files | 30 days. The server removes older files when it starts, except a file that holds a stash copy that is not purged yet. |
| Spill files (responses that were too large) | 7 days. When it starts, the server removes older files whose name is `<32 lowercase hex characters>.json`. It leaves every other file in the folder alone. |
| In-flight records | 7 days. |
| Checkpoint files and stash copies | Until you run `journal.purge_checkpoints` or delete them. |

### Who can read the files

The server and the bridge create their files so that only your user account can read them.

| New file or directory | Mode |
| --- | --- |
| `bridge.log` and its rotated copies | 0600. At start-up the bridge also sets an existing `bridge.log` and `bridge.log.1` to `bridge.log.3` to 0600. |
| `logs` directory | 0700 |
| Journal, spill and in-flight files | 0600 |
| Journal, spill and in-flight directories | 0700 |
| Batch checkpoint directories, made inside Flame | 0700 |
| Batch checkpoint `.batch` files | 0600 |

The server does not change the mode of a journal, spill, `logs` or checkpoint directory that already exists.

## Trust boundaries

### Other programs on your account

The bridge listens on a Unix socket with mode 0600, in a directory with mode 0700. It runs the code it receives, and it does not authenticate the sender. A process that runs as your user can connect to it and send code.

The policy limits only calls that come through the MCP server. It does not limit another process of the same user, including an assistant that has a shell. Do not give an assistant you do not trust a shell on the machine where Flame runs.

### The bridge file

Before the stub in `/opt/Autodesk/shared/python` loads `impl.py`, it checks that:

* `impl.py` and the directory that holds it are not symlinks.
* Your user owns both.
* Neither is writable by group or other (`mode & 0o022` is 0).

It opens `impl.py` without following links and checks the opened file again. If a check fails, the stub does not load the file. **Show last error** in the **MCP Bridge** menu gives the reason. **Reload bridge** makes the same checks. `FLAME_MCP_BRIDGE_DIR` moves the body for development, and the checks apply to it too.

On a machine with other users, also check who can write to `/opt/Autodesk/shared/python`, because every user and every Flame version reads the stub from there.

### Data that reaches the assistant

The server does not filter what Flame returns. Clip names, metadata, setup contents and lines from `app.log` reach the assistant as they are. Text placed in them can try to steer the assistant (prompt injection). When you inspect a project that someone else made, set `FLAME_MCP_READONLY=1`.

### Hints to the client

The server's instructions tell the assistant to ask you to confirm before an operation that cannot be undone or that overwrites something. The MCP tool annotations say that `flame_version_info`, `flame_catalog` and `flame_context` are read-only (`readOnlyHint=true`). They say that `flame_do` is not read-only and can be destructive (`readOnlyHint=false`, `destructiveHint=true`), because it runs whatever operation it is given. These are hints. Your MCP client decides whether and how to ask you.

## Free-form Python (`eval.run`)

`eval.run` runs Python on Flame's main thread when no dedicated operation does the job. Use it last. It is off by default.

> [!CAUTION]
> `eval.run` runs any Python with the permissions of the user who runs Flame. That includes reading, writing and deleting files, network access and starting processes. It is not limited to the Flame API.

### Turn it on

Set `FLAME_MCP_ENABLE_EVAL=1` and leave `FLAME_MCP_READONLY` unset. Only then does `eval.run` appear in `flame_catalog` and in the `op` list that `flame_do` registers, and only then can a call run.

In every other case, the server refuses `eval.run` with `POLICY_DENIED`. This includes a call that declares `read`, and it includes a step of `plan.run`. The `hint` names the switch to set, or says to unset `FLAME_MCP_READONLY`.

### How a call is gated

Each call declares an `effect`, either `read` or `destructive`. Once `eval.run` is on, that declaration decides how the call is gated.

| declared `effect` | allowed when |
| --- | --- |
| `read` | The `eval` category is allowed, and `FLAME_MCP_ALLOW_EFFECTS`, if set, includes `read`, because the operation `eval.run` itself has the effect `read`. |
| `destructive` | The `eval` category is allowed, `FLAME_MCP_ALLOW_EFFECTS`, if set, includes both `read` and `destructive`, and the call has a `target` Batch group. |

Allowing a `read` call does not mean reading is safe. The declaration is a claim by the caller, and the server cannot check it.

A destructive run first checkpoints the `target` Batch group and journals an intent with the code, its SHA-256 hash and the group's fingerprint. After the run, it journals the outcome. `journal.revert(group)` restores the checkpoint while the group still has the fingerprint the run left. The checkpoint covers the `target` group only. A destructive `eval.run` cannot be a step of a plan.

### The check before sending

Before anything goes to Flame, the server scans the code and refuses it with `INVALID_ARGS` if it breaks a rule. The error names the rule and the line.

| rule | refused |
| --- | --- |
| `syntax` | Code that does not parse, or is longer than 50,000 characters. |
| `flame.projects` | Any use of `flame.projects` other than `flame.projects.ATTRIBUTE`: iterating, indexing, `len()` or aliasing. |
| `shortcut` | `execute_shortcut`, because it can send Undo. |
| `thread` | `schedule_idle_event`, `threading`, `_thread` and `QTimer`. Flame calls must go through the bridge's main-thread path. |
| `dynamic-exec` | `exec`, `eval`, `compile`, `__import__`, `importlib`, `subprocess`, and `os.system` and the other `os` calls that start a process. |
| `process` | `flame.exit` and `flame.execute_command`, whatever the effect. |
| `write-in-read` | With `effect='read'` only: a call to a writing method (for example `set_value`, `create_*`, `delete`, `copy`, `load_setup`), an assignment to an object's attribute, `setattr` and `delattr`. |

The scan is accident prevention. It is not a sandbox. It reads names, not behavior, and a passed scan proves nothing about the code. For example, all of these pass it:

* Writing a file with `open(...).write(...)`, or removing files with `shutil`.
* Network access with `urllib` or a socket.
* Importing a module under another name.
* Building a method name at run time, as in `getattr(x, 'set_' + 'value')(...)`. This passes the `write-in-read` rule, so a call that declares `read` can still write.
* An endless loop such as `while True`.

The rules stop writes that are visible in the code. They cannot stop every write. If nothing may write through the server, leave `eval.run` off, and set `FLAME_MCP_READONLY=1` to refuse it for good.

A refused call stops with nothing sent. The timeout is at most 120 seconds, because the code runs on Flame's main thread and freezes the interface while it runs. `timeout_s` only makes the server stop waiting. It does not stop the code. Code that does not end keeps running on the main thread and can freeze Flame. The server has no operation that stops code that is already running.

The description of `eval.run` also tells the assistant not to build a non-ASCII string at run time and pass it to Flame (see [ASCII only](#ascii-only)). The server does not refuse that.

## Safety in real-Flame tests

The test suite includes scripts that drive a real Flame. They write to the project, so they follow these rules.

* **Use a dedicated project.** The end-to-end runner starts Flame in a project named `flame_mcp_probe`. Each end-to-end client first reads the `projectName` in the bridge's `hello` reply. If it is not `flame_mcp_probe`, the client creates nothing, sends no `flame.exit()` and no kill, and stops with exit code 2. The clean-up script deletes nothing and does not close Flame in any other project. The name `flame_mcp_probe` is the default. To use another name for the dedicated project, set `FLAME_MCP_E2E_PROJECT`. The name must be `flame_mcp_probe` or start with `flame_mcp_probe_` (for example `flame_mcp_probe_2026`); any other value stops the script. The runner, every client and the clean-up script then use that name, and a project must match it exactly. Never run any script from `scripts/` in a project you work in.
* **Eval is on for the tests.** The child server of an end-to-end run sets `FLAME_MCP_ENABLE_EVAL=1` so that `eval.run` is available, except in the read-only stages.
* **Delete only what the tests made.** Test objects get a name made of a fixed prefix and a time stamp. `scripts/p2_4_clean_probe_project.py` deletes only objects whose names match that form. It lists everything else as `untouched`. It does not touch projects, shared libraries or files on disk.
* **Look before you delete.** Run the clean-up script in `dry-run` mode first. It prints what it would delete, keep and create. Then run it in `apply` mode.
* **Clean up after every run.** `scripts/regress.sh` runs the end-to-end scripts in turn and cleans the test project at the end. Each script starts its own Flame with a development bridge. `regress.sh` stops if Flame is already running.
* **Keep the fault-injection switches off.** `FLAME_MCP_TEST_PAUSE_AFTER_PREREAD`, `FLAME_MCP_TEST_EXIT_AFTER_WRITE` and `FLAME_MCP_TEST_WRITE_TIMEOUT_S` make a write stop halfway or the server die. The tests set them per process. Never set them in the environment you use to register the server. `scripts/flame_mcp_server.sh` does not set them.

To clean up the test project by hand:

1. Quit Flame. The script starts its own Flame and stops if one is already running.
2. Run the clean-up script in dry-run mode. The number 900 is the time in seconds to wait for Flame to exit.

   ```bash
   scripts/run_flame_with_bridge.sh scripts/p2_4_clean_probe_project.py 900 -- --mode dry-run
   ```

3. Read the printed lists of what it would delete, keep and create.
4. If the lists are right, run it again in apply mode.

   ```bash
   scripts/run_flame_with_bridge.sh scripts/p2_4_clean_probe_project.py 900 -- --mode apply
   ```

## Tested range

* **Operating system:** macOS. Linux is not supported.
* **Flame:** 2025.2.4 and 2026.2.3. The end-to-end tests passed on both. The Flame behavior described on this page was measured on 2025.2.4 only; the individual measurements were not repeated on 2026.2.3.
* **Other Flame versions:** The bridge is written to start and to warn that the version is untested. The warning appears in `flame_version_info` (`bridge.untestedFlameVersion` and `notices`), in the Show status menu item and in the bridge log.
* **The server's own rules:** Statements about the policy, the journal and the checkpoints come from the code and do not depend on the Flame version.
