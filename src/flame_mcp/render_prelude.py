"""Prelude source for the `render` category's Flame-side code (`ops/render.py`).

Like `batch_prelude` / `node_prelude`: real Python source that `execution.build_exec_code` places
ahead of an op's own code, so each op's `to_python` output is a couple of loop-free lines
(registry.py's pacing guard parses only that output). It runs inside Flame (3.11) with `flame`,
`_v`, `_ja`, `_try`, `_sock_state`, `_clip_names` and `_OpError` already defined.

What it encodes (measured on Flame 2025.2.4):
  - a Write File node (`PyWriteFileNode`) writes files: `media_path`, `media_path_pattern`,
    `file_type`, `format_extension`, `range_start` / `range_end` are attributes, and
    `get_resolved_media_path()` says where -- `/var/tmp/writefile1.[000001-000100].exr`. The
    frame count of a render is its range (default 1-100), not the group's duration.
  - a Render node (`PyRenderNode`) has 23 attributes and no path: no `get_resolved_media_path`,
    no `media_path`. Its `destination` (['Batch Reels', 'Batch Renders']) and
    `tape_name` say where its clip goes; the API shows nothing that can be checked on disk.
  - `PyBatch.render(render_option='Foreground')` works on a group that is not current and blocks
    Flame's main thread until it is done (~4-5 s for 24-100 frames of 1080p EXR). A
    render puts a clip into the shelf reel `Batch Renders` and writes a `.clip` file
    beside the frames.
  - a Write File is effective only if it is not bypassed and something is wired to its input.
"""

from __future__ import annotations

RENDER_NODE_PY_TYPES: tuple[str, ...] = ("PyWriteFileNode", "PyRenderNode")

RENDER_HELPER = """
_RENDER_PY_TYPES = ('PyWriteFileNode', 'PyRenderNode')


def _attr_or_none(node, key):
    return _try(lambda: _ja(getattr(node, key)))


def _wired_inputs(node):
    try:
        state = _sock_state(node)
    except BaseException:
        return None
    return sum(len(v) for v in state['input'].values())


def _render_node_row(node):
    py_type = type(node).__name__
    is_writer = py_type == 'PyWriteFileNode'
    row = {
        'name': _try(lambda: _v(node.name)),
        'pyType': py_type,
        'kind': 'write_file' if is_writer else 'render',
        'bypass': _attr_or_none(node, 'bypass'),
        'wiredInputs': _wired_inputs(node),
        'rangeStart': _attr_or_none(node, 'range_start'),
        'rangeEnd': _attr_or_none(node, 'range_end'),
    }
    if is_writer:
        row['mediaPath'] = _attr_or_none(node, 'media_path')
        row['mediaPathPattern'] = _attr_or_none(node, 'media_path_pattern')
        row['fileType'] = _attr_or_none(node, 'file_type')
        row['formatExtension'] = _attr_or_none(node, 'format_extension')
        row['framePadding'] = _attr_or_none(node, 'frame_padding')
        row['createClip'] = _attr_or_none(node, 'create_clip')
        row['resolvedMediaPath'] = _try(lambda: _ja(node.get_resolved_media_path()))
    else:
        row['destination'] = _attr_or_none(node, 'destination')
        row['tapeName'] = _attr_or_none(node, 'tape_name')
    return row


def _render_reel_state(batch):
    rows = []
    for attr, kind in (('reels', 'schematic'), ('shelf_reels', 'shelf')):
        for r in list(getattr(batch, attr)):
            rows.append(
                {
                    'kind': kind,
                    'name': _try(lambda r=r: _v(r.name)),
                    'clips': _clip_names(r),
                }
            )
    return rows


def _render_reel_counts(rows):
    return [
        {'kind': r['kind'], 'name': r['name'],
         'clipCount': None if r['clips'] is None else len(r['clips'])}
        for r in rows
    ]


def _render_plan(batch):
    rows = [_render_node_row(n) for n in list(batch.nodes) if type(n).__name__ in _RENDER_PY_TYPES]
    return {
        'batch': _batch_ident(batch),
        'stable': _batch_selector(batch),
        'nodes': rows,
        'reels': _render_reel_counts(_render_reel_state(batch)),
    }


def _render_new_clips(before, after):
    added = []
    for row in after:
        old = [b for b in before if b['kind'] == row['kind'] and b['name'] == row['name']]
        seen = [] if not old or old[0]['clips'] is None else list(old[0]['clips'])
        fresh = []
        for clip in row['clips'] or []:
            if clip in seen:
                seen.remove(clip)
            else:
                fresh.append(clip)
        if fresh:
            added.append({'kind': row['kind'], 'name': row['name'], 'clips': fresh})
    return added


def _render_run(batch):
    import time
    before = _render_reel_state(batch)
    started = time.monotonic()
    returned = batch.render(render_option='Foreground')
    elapsed = time.monotonic() - started
    after = _render_reel_state(batch)
    return {
        'renderOption': 'Foreground',
        'returned': _ja(returned),
        'elapsedS': round(elapsed, 3),
        'newClips': _render_new_clips(before, after),
        'reelsBefore': _render_reel_counts(before),
        'reelsAfter': _render_reel_counts(after),
    }
"""
