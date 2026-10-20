"""Turns an op's `to_python` body into the single generated script sent to the bridge.

`transport.exec` sends that script (op + verify + rider in one main callback).

Contract with op authors (op.to_python / op.verify):
  - `to_python(args)` sets `_result` (its own value, not the envelope) and
    may set `_affected` (an int) and `_partial` (`{str: int}`, e.g. how many
    writes landed / failed -- it reaches the envelope's `partial`). Both run
    inside a `try/except BaseException` this module writes, not the op's own.
    An op that fails after assigning `_result` may `raise _OpError(...)`: the
    value is kept and reaches the envelope's `result` beside the error.
  - `verify(args)`, if declared, sets `_verified: bool` and may set
    `_verify_detail`. It only runs when the op raised nothing.
  - Both bodies may call the module-level helper `_v(x)`, defined by this
    wrapper, to read a value that may be a plain str or a PyAttribute
    (PyProject.name and PyUser.name are plain str on Flame 2025.2.4; most other
    `.name` attributes are PyAttribute).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flame_mcp import envelope as envelope_mod
from flame_mcp import transport
from flame_mcp.attribute_prelude import ATTRIBUTE_HELPER
from flame_mcp.batch_prelude import BATCH_HELPER
from flame_mcp.checkpoint_prelude import CHECKPOINT_HELPER
from flame_mcp.desktop_batch_prelude import BATCH_COPY_HELPER
from flame_mcp.desktop_delete_prelude import DELETE_HELPER, PURGE_HELPER
from flame_mcp.desktop_media_prelude import MEDIA_HELPER
from flame_mcp.desktop_prelude import DESKTOP_HELPER
from flame_mcp.desktop_reformat_prelude import REFORMAT_HELPER
from flame_mcp.library_open import has_refs, library_refs
from flame_mcp.library_open_prelude import LIBRARY_OPEN_HELPER
from flame_mcp.node_prelude import ACTION_HELPER, NODE_HELPER
from flame_mcp.read_prelude import READ_HELPER
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_prelude import RENDER_HELPER
from flame_mcp.segment_connection_prelude import CONNECTION_HELPER
from flame_mcp.segment_extract_prelude import EXTRACT_HELPER
from flame_mcp.timeline_build_prelude import BUILD_HELPER
from flame_mcp.timeline_compare_prelude import COMPARE_HELPER
from flame_mcp.timeline_create_prelude import CREATE_HELPER
from flame_mcp.timeline_delete_prelude import DELETE_SEGMENTS_HELPER
from flame_mcp.timeline_edit_prelude import EDIT_HELPER
from flame_mcp.timeline_move_prelude import MOVE_TRACK_HELPER
from flame_mcp.timeline_prelude import TIMELINE_HELPER
from flame_mcp.timeline_shorten_prelude import SHORTEN_HELPER
from flame_mcp.timeline_trim_prelude import TRIM_HELPER

V_HELPER = "def _v(x):\n    return x.get_value() if type(x).__name__ == 'PyAttribute' else x"
"""The `_v` prelude.

Shared verbatim by `build_exec_code` and `server._CONTEXT_CODE`, so it must exist in
exactly one place.

The check is on the type name, not `hasattr(x, 'get_value')`. Probed on Flame 2025.2.4:
all 17 PyFlameObject subclasses tried answer `None` for any name they do not define
(a catch-all `__getattr__`), so `hasattr` is always true for them and `_v(x)` would call
`None()` for anything that is not a PyAttribute. Same reasoning as `_j`'s PyTime check below."""

J_HELPER = "\n".join(
    [
        "def _j(x):",
        "    __v = _v(x)",
        "    if __v is None or isinstance(__v, (str, int, float, bool)):",
        "        return __v",
        "    if type(__v).__name__ == 'PyTime':",
        "        return {'timecode': _j(__v.timecode), 'frame': _j(__v.frame)}",
        "    return repr(__v)",
    ]
)
"""JSON-safe value extraction for op result dicts (e.g. PyTime fields like record_in/record_out).

`_v` first (PyAttribute -> its value). A PyTime (no `get_value`, but has both
`timecode` and `frame`) becomes `{"timecode": ..., "frame": ...}` -- recursing through `_j` again so
neither member can reintroduce quoting. Anything else not a plain JSON
scalar falls back to `repr()`, never `str()`: on Flame 2025.2.4
`str()` of a PyAttribute was quoted (a library name came back as `"'P1E_260927_172550'"`)
while `repr()` was plain.

The PyTime check is on the type name, not
`hasattr(__v, 'timecode') and hasattr(__v, 'frame')`: PyFlameObject
subclasses return `None` from an unknown-attribute `__getattr__`
instead of raising `AttributeError`, so `hasattr` is `True` for names they
never actually define and any such object would look PyTime-like. Matching on
`type(__v).__name__ == 'PyTime'` only fires for the real type."""

OP_ERROR_HELPER = "\n".join(
    [
        "class _OpError(Exception):",
        "    def __init__(self, code, message):",
        "        super().__init__(message)",
        "        self.code = code",
        "        self.message = message",
    ]
)
"""Raised by an op's `to_python` / selector-resolution code to report a
specific ErrorCode (SELECTOR_EMPTY/SELECTOR_TYPE/SELECTOR_AMBIGUOUS) instead of falling
through to the generic FLAME_API_ERROR every other exception maps to."""

SELECTOR_HELPER = "\n".join(
    [
        "def _resolve_selection():",
        "    entries = flame.media_panel.selected_entries",
        "    if len(entries) == 0:",
        "        raise _OpError('SELECTOR_EMPTY', 'selection selector matched 0 entries')",
        "    if len(entries) > 1:",
        "        raise _OpError(",
        "            'SELECTOR_AMBIGUOUS',",
        "            'expected exactly 1 selected entry, got ' + str(len(entries)),",
        "        )",
        "    return entries[0]",
        "",
        "",
        "def _resolve_wiretap(wiretap_id):",
        "    try:",
        "        found = flame.find_by_wiretap_node_id(wiretap_id)",
        "    except BaseException as exc:",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'find_by_wiretap_node_id(' + repr(wiretap_id) + ') raised: ' + str(exc),",
        "        )",
        "    if found is None:",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY', 'no object found for wiretap id ' + repr(wiretap_id)",
        "        )",
        "    return found",
        "",
        "",
        "def _list_or_none(obj, attr):",
        "    try:",
        "        value = getattr(obj, attr)",
        "        if value is None:",
        "            return None",
        "        return list(value)",
        "    except BaseException:",
        "        return None",
        "",
        "",
        "def _desktop_sources(cur):",
        "    # Unconfirmed on a real Flame: whether Desktop.children holds the",
        "    # Reel Groups / Batch groups, and whether a Reel Group's children hold its reels.",
        "    # So each step tries `children` first and, when nothing matches there, the",
        "    # narrower accessors below. The first source with a match wins.",
        "    if cur is None:",
        "        desktop = flame.projects.current_project.current_workspace.desktop",
        "        fallback = (_list_or_none(desktop, 'reel_groups') or []) + (",
        "            _list_or_none(desktop, 'batch_groups') or []",
        "        )",
        "        return [_list_or_none(desktop, 'children') or [], fallback]",
        "    fallback = (_list_or_none(cur, 'clips') or []) + (",
        "        _list_or_none(cur, 'sequences') or []",
        "    )",
        "    return [",
        "        _list_or_none(cur, 'children') or [],",
        "        _list_or_none(cur, 'reels') or [],",
        "        fallback,",
        "    ]",
        "",
        "",
        "def _resolve_path(names, root='libraries'):",
        "    cur = None",
        "    for i, name in enumerate(names):",
        "        if root == 'desktop':",
        "            sources = _desktop_sources(cur)",
        "        elif i == 0:",
        "            sources = [flame.projects.current_project.current_workspace.libraries]",
        "        else:",
        "            sources = [cur.children]",
        "        matches = []",
        "        for source in sources:",
        "            matches = [c for c in source if _v(c.name) == name]",
        "            if matches:",
        "                break",
        "        if len(matches) == 0:",
        "            raise _OpError(",
        "                'SELECTOR_EMPTY',",
        "                'no match for ' + repr(name) + ' at path step ' + str(i),",
        "            )",
        "        if len(matches) > 1:",
        "            raise _OpError(",
        "                'SELECTOR_AMBIGUOUS',",
        "                'ambiguous: '",
        "                + str(len(matches))",
        "                + ' matches for '",
        "                + repr(name)",
        "                + ' at path step '",
        "                + str(i),",
        "            )",
        "        cur = matches[0]",
        "    return cur",
        "",
        "",
        "def _segment_list(parent, version, track):",
        "    versions = list(parent.versions)",
        "    if version >= len(versions):",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'version index '",
        "            + str(version)",
        "            + ' out of range (have '",
        "            + str(len(versions))",
        "            + ')',",
        "        )",
        "    tracks = list(versions[version].tracks)",
        "    if track >= len(tracks):",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'track index '",
        "            + str(track)",
        "            + ' out of range (have '",
        "            + str(len(tracks))",
        "            + ')',",
        "        )",
        "    return list(tracks[track].segments)",
        "",
        "",
        "def _expect_source(segment, expect):",
        "    if expect is None:",
        "        return segment",
        "    actual = _v(segment.source_name)",
        "    if actual != expect:",
        "        raise _OpError(",
        "            'CONFLICT',",
        "            'the segment found has source_name '",
        "            + repr(actual)",
        "            + ', not the expected '",
        "            + repr(expect)",
        "            + '; nothing was done',",
        "        )",
        "    return segment",
        "",
        "",
        "def _resolve_segment(parent, version, track, index, expect=None):",
        "    segments = _segment_list(parent, version, track)",
        "    if index >= len(segments):",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'segment index '",
        "            + str(index)",
        "            + ' out of range (have '",
        "            + str(len(segments))",
        "            + ')',",
        "        )",
        "    return _expect_source(segments[index], expect)",
        "",
        "",
        "def _resolve_segment_at(parent, version, track, record_in, expect=None):",
        "    segments = _segment_list(parent, version, track)",
        "    matches = [s for s in segments if s.record_in.frame == record_in]",
        "    if len(matches) == 0:",
        "        frames = [s.record_in.frame for s in segments]",
        "        near = sorted(frames, key=lambda f: (abs(f - record_in), f))[:5]",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'no segment with record_in '",
        "            + str(record_in)",
        "            + ' in this track (nearest record_in values: '",
        "            + str(near)",
        "            + ')',",
        "        )",
        "    what = 'segments with record_in ' + str(record_in) + ' in this track'",
        "    found = _pick_one(matches, what)",
        "    return _expect_source(found, expect)",
        "",
        "",
        "def _pick_one(matches, what, describe=None, advice=''):",
        "    if len(matches) == 0:",
        "        raise _OpError('SELECTOR_EMPTY', 'no ' + what)",
        "    if len(matches) > 1:",
        "        text = 'ambiguous: ' + str(len(matches)) + ' ' + what",
        "        if describe is not None:",
        "            text += '; candidates: ' + '; '.join(describe(m) for m in matches[:10])",
        "            if len(matches) > 10:",
        "                text += '; and ' + str(len(matches) - 10) + ' more'",
        "        raise _OpError('SELECTOR_AMBIGUOUS', text + advice)",
        "    return matches[0]",
        "",
        "",
        "def _batch_candidate(g):",
        "    return 'name=' + repr(_v(g.name)) + ' uid=' + repr(_v(g.uid))",
        "",
        "",
        "def _resolve_batch(uid, name):",
        "    desktop = flame.projects.current_project.current_workspace.desktop",
        "    groups = list(desktop.batch_groups)",
        "    if uid is not None:",
        "        matches = [g for g in groups if _v(g.uid) == uid]",
        "        return _pick_one(matches, 'Batch group with uid ' + repr(uid), _batch_candidate)",
        "    matches = [g for g in groups if _v(g.name) == name]",
        "    return _pick_one(matches, 'Batch group named ' + repr(name), _batch_candidate)",
        "",
        "",
        "def _resolve_desktop():",
        "    return flame.projects.current_project.current_workspace.desktop",
        "",
        "",
        "def _resolve_timeline():",
        "    try:",
        "        shown = flame.timeline.clip",
        "    except BaseException as exc:",
        "        raise _OpError(",
        "            'SELECTOR_EMPTY',",
        "            'the Timeline selector could not read what it shows: ' + str(exc),",
        "        )",
        "    if shown is None:",
        "        raise _OpError('SELECTOR_EMPTY', 'the Timeline shows nothing')",
        "    return shown",
        "",
        "",
        "def _pos_number(value):",
        "    value = _v(value)",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):",
        "        return None",
        "    if isinstance(value, float) and value.is_integer():",
        "        return int(value)",
        "    return value",
        "",
        "",
        "def _node_pos(n):",
        "    try:",
        "        x = _pos_number(n.pos_x)",
        "        y = _pos_number(n.pos_y)",
        "    except BaseException:",
        "        return None",
        "    if x is None or y is None:",
        "        return None",
        "    return [x, y]",
        "",
        "",
        "def _node_candidate(n):",
        "    try:",
        "        ntype = _v(n.type)",
        "    except BaseException:",
        "        ntype = None",
        "    return (",
        "        'name=' + repr(_v(n.name)) + ' type=' + repr(ntype)",
        "        + ' pos=' + repr(_node_pos(n))",
        "    )",
        "",
        "",
        "def _resolve_node(batch, name, pos=None):",
        "    matches = [n for n in list(batch.nodes) if _v(n.name) == name]",
        "    what = 'node named ' + repr(name) + ' in the Batch group'",
        "    if pos is not None and matches:",
        "        at = [n for n in matches if _node_pos(n) == pos]",
        "        if len(at) == 0:",
        "            raise _OpError(",
        "                'SELECTOR_EMPTY',",
        "                'no node named ' + repr(name) + ' at pos ' + repr(pos)",
        "                + ' (the nodes of that name are at '",
        "                + repr([_node_pos(n) for n in matches[:10]]) + ')',",
        "            )",
        "        matches = at",
        "    return _pick_one(matches, what, _node_candidate, '. Pass pos to choose one')",
        "",
        "",
        "def _resolve_action_node(node, name):",
        "    try:",
        "        inner = list(node.nodes)",
        "    except BaseException as exc:",
        "        raise _OpError(",
        "            'SELECTOR_TYPE',",
        "            'node ' + repr(_v(node.name)) + ' has no inner nodes (not an Action, '",
        "            'Image or GMask Tracer node?): ' + str(exc),",
        "        )",
        "    matches = [n for n in inner if _v(n.name) == name]",
        "    return _pick_one(matches, 'inner node named ' + repr(name))",
    ]
)
"""Selector resolution, as real Python functions in the trusted prelude.

It is not inlined into each op's own `to_python` output.
`selectors.resolve_code` only emits a single-line (or, for `segment`, two-line)
function-call assignment.

Why this matters for registry.py's registration-time pacing/loop guard: that
guard `ast.parse`s only the op's *own* `to_python(sample_args)` output, never
this prelude. If `path`'s child-filtering list comprehension were generated
directly into the op's own code:
  - a write op whose `path` resolution wasn't the sample the registry
    happened to test (whichever `accepts[0]` was) could hide an unrelated
    loop from the pacing guard entirely (a real detection gap, not a safety
    valve)
  - conversely, `path`'s own loop (a harmless one-shot filter over an
    already-fetched list, not a repeated write API call) could trip the
    *same* guard as a false positive purely because of which kind happened
    to be sampled first
Keeping resolution in this prelude leaves no loop AST node in the op's own
generated code for any selector kind, so neither failure mode is possible.
registry.py additionally tests every `accepts` kind, not just the first."""

DESCRIBE_ENTRY_HELPER = "\n".join(
    [
        "def _describe_entry(obj):",
        "    entry_type = type(obj).__name__",
        "    try:",
        "        name = _v(obj.name)",
        "    except BaseException:",
        "        name = None",
        "    uid = None",
        "    try:",
        "        if 'uid' in list(obj.attributes):",
        "            uid = _v(obj.uid)",
        "    except BaseException:",
        "        uid = None",
        "    wiretap_id = None",
        "    try:",
        "        wiretap_id = obj.get_wiretap_node_id()",
        "    except BaseException:",
        "        wiretap_id = None",
        "    entry = {'type': entry_type, 'name': name, 'uid': uid, 'wiretapId': wiretap_id}",
        "    if entry_type in ('PyClip', 'PySequence'):",
        "        try:",
        "            entry['duration'] = _j(obj.duration)",
        "        except BaseException:",
        "            entry['duration'] = None",
        "        try:",
        "            entry['frameRate'] = _j(obj.frame_rate)",
        "        except BaseException:",
        "            entry['frameRate'] = None",
        "    return entry",
    ]
)
"""Shared by the read ops.

Lives in the prelude next to `_resolve_*` rather than being spliced into each op's own
to_python output: one place for every op-support function, like `_v`/`_j`/`_OpError`.
"""


def build_exec_code(op: RegisteredOperation, args: Mapping[str, Any]) -> str:
    """Assembles the full script sent to `transport.exec` for `op`.

    Structure:
      1. `_v` helper
      2. op code, run via `exec(compile(...), globals())` inside try/except
         -> `__op_error` / `__op_traceback`
      3. verify code (if declared), only on op success -> `_verified` / `_verify_detail`
      4. rider fields, each in its own try/except -> None on failure
      5. final `_result = {...}`, which overwrites the op's own `_result`, whose
         value was already captured into `__op_value` first (never lost).

    The op's (and verify's) own source is never re-indented and spliced into this
    script as literal text, because that would silently corrupt any multi-line
    string literal the op's code contains (textwrap.indent has no notion of
    "inside a triple-quoted string").
    Instead each is embedded as a `repr()`-escaped string constant and run
    with its own `compile(..., "<op:NAME>" | "<verify:NAME>", "exec")`, so
    (a) the source text `op.to_python`/`op.verify` returned is preserved
    byte-for-byte, and (b) a traceback from the op's own code names that
    op/verify and its own line number, not this wrapper's.
    """
    if op.to_python is None:
        raise ValueError(f"'{op.name}': thread='{op.thread}' operation has no to_python to run")

    default_affected = 0 if op.effect == "read" else 1
    op_src = op.to_python(args)
    op_filename = f"<op:{op.name}>"

    lines: list[str] = [
        V_HELPER,
        "",
        J_HELPER,
        "",
        OP_ERROR_HELPER,
        "",
        SELECTOR_HELPER,
        "",
        DESCRIBE_ENTRY_HELPER,
        "",
        ATTRIBUTE_HELPER.strip("\n"),
        "",
        CHECKPOINT_HELPER.strip("\n"),
        "",
        BATCH_HELPER.strip("\n"),
        "",
        NODE_HELPER.strip("\n"),
        "",
        ACTION_HELPER.strip("\n"),
        "",
        RENDER_HELPER.strip("\n"),
        "",
        DESKTOP_HELPER.strip("\n"),
        "",
        MEDIA_HELPER.strip("\n"),
        "",
        BATCH_COPY_HELPER.strip("\n"),
        "",
        LIBRARY_OPEN_HELPER.strip("\n"),
        "",
        READ_HELPER.strip("\n"),
        "",
        TIMELINE_HELPER.strip("\n"),
        "",
        TRIM_HELPER.strip("\n"),
        "",
        EDIT_HELPER.strip("\n"),
        "",
        BUILD_HELPER.strip("\n"),
        "",
        CREATE_HELPER.strip("\n"),
        "",
        EXTRACT_HELPER.strip("\n"),
        "",
        CONNECTION_HELPER.strip("\n"),
        "",
        DELETE_SEGMENTS_HELPER.strip("\n"),
        "",
        MOVE_TRACK_HELPER.strip("\n"),
        "",
        SHORTEN_HELPER.strip("\n"),
        "",
        COMPARE_HELPER.strip("\n"),
        "",
        DELETE_HELPER.strip("\n"),
        "",
        PURGE_HELPER.strip("\n"),
        "",
        REFORMAT_HELPER.strip("\n"),
        "",
    ]

    refs = library_refs(args)
    open_lines = (
        [f"__lib_refs = {refs!r}", "__lib_state = {'opened': []}"] if has_refs(refs) else []
    )
    open_call = ["    _lo_open(__lib_refs, __lib_state)"] if open_lines else []
    lines += [
        "_result = None",
        "_affected = None",
        "_partial = None",
        "__op_error = None",
        "__op_error_code = None",
        "__op_traceback = None",
        *open_lines,
        f"__op_src = {op_src!r}",
        "try:",
        *open_call,
        f"    exec(compile(__op_src, {op_filename!r}, 'exec'), globals())",
        "except _OpError as __oe:",
        "    __op_error_code = __oe.code",
        "    __op_error = __oe.message",
        "except BaseException:",
        "    import traceback as __tb_op",
        "    __op_traceback = __tb_op.format_exc()",
        "    __op_error = __op_traceback.strip().splitlines()[-1]",
        "",
        "__op_value = _result",
        "__op_partial = _partial",
        f"__op_affected = _affected if _affected is not None else "
        f"({default_affected!r} if __op_error is None else 0)",
        "",
        "_verified = None",
        "_verify_detail = None",
    ]

    if op.verify is not None:
        verify_src = op.verify(args)
        if verify_src is None:
            raise ValueError(f"'{op.name}': verify(args) returned None instead of code")
        verify_filename = f"<verify:{op.name}>"
        lines += [
            f"__verify_src = {verify_src!r}",
            "if __op_error is None:",
            "    try:",
            f"        exec(compile(__verify_src, {verify_filename!r}, 'exec'), globals())",
            "    except BaseException:",
            "        import traceback as __tb_verify",
            "        _verified = False",
            "        _verify_detail = __tb_verify.format_exc().strip().splitlines()[-1]",
        ]

    if open_lines:
        lines += [
            "",
            "__op_value = _lo_finish(__op_value, __lib_state)",
        ]

    lines += [
        "",
        "__ctx_project = None",
        "try:",
        "    __ctx_project = flame.projects.current_project.name",
        "except BaseException:",
        "    __ctx_project = None",
        "",
        "__ctx_workspace = None",
        "try:",
        "    __ctx_workspace = _v(flame.projects.current_project.current_workspace.name)",
        "except BaseException:",
        "    __ctx_workspace = None",
        "",
        "__ctx_selected = None",
        "try:",
        "    __ctx_selected = len(flame.media_panel.selected_entries)",
        "except BaseException:",
        "    __ctx_selected = None",
        "",
        "_result = {",
        "    'value': __op_value,",
        "    'affected': __op_affected,",
        "    'partial': __op_partial,",
        "    'opError': __op_error,",
        "    'opErrorCode': __op_error_code,",
        "    'opTraceback': __op_traceback,",
        "    'verified': _verified,",
        "    'verifyDetail': _verify_detail,",
        "    'context': {",
        "        'project': __ctx_project,",
        "        'workspace': __ctx_workspace,",
        "        'selected': __ctx_selected,",
        "    },",
        "}",
    ]
    return "\n".join(lines)


def run_op(
    op: RegisteredOperation,
    args: Mapping[str, Any],
    *,
    run_dir: Path | None = None,
    timeout_s: float | None = None,
    handle: str | None = None,
) -> dict[str, Any]:
    """Builds the wrapped script, sends it through `transport.exec`, and maps the result.

    The result is mapped onto the response envelope. `op.thread == "server"` never
    reaches here; if it does, that is a bug in this server's own dispatch
    (`runtime.Runtime` runs server-thread operations itself), not a user error, so it
    raises rather than mapping to any ErrorCode. `timeout_s` overrides the thread's
    default (only the test hook `FLAME_MCP_TEST_WRITE_TIMEOUT_S` passes it). `handle`
    is a caller-chosen bridge handle for a `main-async` op (see `transport.exec`); it is
    passed on only when set.
    """
    if op.thread == "server":
        raise RuntimeError(
            f"'{op.name}': thread='server' operations run on the server (runtime.Runtime), "
            "never through the bridge, so this is a server bug"
        )
    code = build_exec_code(op, args)
    extra: dict[str, Any] = {"handle": handle} if handle is not None else {}
    br = transport.exec(
        code,
        thread=op.thread,
        timeout_s=timeout_s if timeout_s is not None else transport.timeout_for(op.thread),
        label=op.name,
        run_dir=run_dir,
        **extra,
    )
    return envelope_mod.from_bridge_result(op, args, br)
