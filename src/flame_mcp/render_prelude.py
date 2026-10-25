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
  - a Render node whose `destination` is ['Reel Groups', <group>] puts its clip, named after the
    node, into a reel of the Desktop's reel group <group> -- not into a reel of the Batch group
    -- and `newClips` / the Batch group's reels show nothing of it. Which reel depends on the
    Flame version. On 2025.2.4 the destination reads back as those 2 elements and the reel is
    `Batch Renders` (Flame made it when the group had none; a second render went into the same
    reel): `DESKTOP_RENDER_REEL`, one constant. On 2026.2.3 setting the same 2 elements reads
    back as 3, ['Reel Groups', <group>, 'Reel 1'], and the clip goes into that reel; no
    `Batch Renders` is made. So a 3rd element that is a string names the reel, and without one the
    reel is `Batch Renders`. `destination.values` cannot be read. Only 'Batch Reels' and
    'Reel Groups' are followed; any other kind is recorded in `destinationUnsupported` and not
    counted.
"""

from __future__ import annotations

RENDER_NODE_PY_TYPES: tuple[str, ...] = ("PyWriteFileNode", "PyRenderNode")

DESKTOP_RENDER_REEL = "Batch Renders"

RENDER_HELPER = (
    f"_DESKTOP_RENDER_REEL = {DESKTOP_RENDER_REEL!r}\n"
    + """
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


def _render_row_effective(row):
    wired = row.get('wiredInputs')
    return row.get('bypass') is not True and isinstance(wired, int) and wired > 0


def _render_destination_lookup(batch, dest):
    if not isinstance(dest, list) or len(dest) < 2 or not isinstance(dest[1], str):
        return None
    kind, name = dest[0], dest[1]
    reel_name = dest[2] if len(dest) > 2 and isinstance(dest[2], str) else _DESKTOP_RENDER_REEL
    if kind == 'Batch Reels':
        info = {'kind': 'shelf', 'group': None, 'reel': name}
        reels = _try(lambda: [r for r in list(batch.shelf_reels) if _v(r.name) == name])
    elif kind == 'Reel Groups':
        info = {'kind': 'desktop', 'group': name, 'reel': reel_name}
        groups = _try(
            lambda: [
                g
                for g in list(flame.projects.current_project.current_workspace.desktop.reel_groups)
                if _v(g.name) == name
            ]
        )
        if groups is None:
            reels = None
        elif not groups:
            info['groupMissing'] = True
            reels = []
        else:
            reels = _try(
                lambda: [r for r in list(groups[0].reels) if _v(r.name) == reel_name]
            )
    else:
        return None
    if reels is None:
        info.update(exists=None, clipCount=None)
        return info, None
    clips = _clip_names(reels[0]) if reels else []
    info.update(exists=bool(reels), clipCount=None if clips is None else len(clips))
    return info, clips


def _render_scan(batch):
    nodes = []
    desktop = []
    unsupported = []
    seen = set()
    for node in list(batch.nodes):
        if type(node).__name__ not in _RENDER_PY_TYPES:
            continue
        row = _render_node_row(node)
        nodes.append(row)
        if row['kind'] != 'render':
            continue
        dest = row['destination']
        found = _render_destination_lookup(batch, dest)
        row['destinationReel'] = None if found is None else found[0]
        if not _render_row_effective(row):
            continue
        if found is None:
            entry = {'node': row['name'], 'destination': dest}
            if dest is None:
                entry['unread'] = True
            unsupported.append(entry)
            continue
        info, clips = found
        if info['kind'] != 'desktop' or (info['group'], info['reel']) in seen:
            continue
        seen.add((info['group'], info['reel']))
        reel_row = {'kind': 'desktop', 'name': info['group'] + '/' + info['reel'], 'clips': clips}
        if info['exists'] is False:
            reel_row['missing'] = True
        if info.get('groupMissing'):
            reel_row['groupMissing'] = True
        desktop.append(reel_row)
    return {'nodes': nodes, 'desktop': desktop, 'unsupported': unsupported}


def _render_reel_state(batch, scan):
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
    return rows + scan['desktop']


def _render_reel_counts(rows):
    counts = []
    for r in rows:
        count = {
            'kind': r['kind'],
            'name': r['name'],
            'clipCount': None if r['clips'] is None else len(r['clips']),
        }
        for flag in ('missing', 'groupMissing'):
            if r.get(flag):
                count[flag] = True
        counts.append(count)
    return counts


def _render_plan(batch):
    scan = _render_scan(batch)
    return {
        'batch': _batch_ident(batch),
        'stable': _batch_selector(batch),
        'nodes': scan['nodes'],
        'reels': _render_reel_counts(_render_reel_state(batch, scan)),
        'destinationUnsupported': scan['unsupported'],
    }


def _render_new_clips(before, after):
    added = []
    unknown = []
    for row in after:
        old = [b for b in before if b['kind'] == row['kind'] and b['name'] == row['name']]
        if old and old[0]['clips'] is None:
            unknown.append({'kind': row['kind'], 'name': row['name']})
            continue
        seen = [] if not old else list(old[0]['clips'])
        fresh = []
        for clip in row['clips'] or []:
            if clip in seen:
                seen.remove(clip)
            else:
                fresh.append(clip)
        if fresh:
            added.append({'kind': row['kind'], 'name': row['name'], 'clips': fresh})
    return added, unknown


def _render_run(batch):
    import time
    scan = _render_scan(batch)
    before = _render_reel_state(batch, scan)
    started = time.monotonic()
    returned = batch.render(render_option='Foreground')
    elapsed = time.monotonic() - started
    after = _render_reel_state(batch, _render_scan(batch))
    added, unknown = _render_new_clips(before, after)
    return {
        'renderOption': 'Foreground',
        'returned': _ja(returned),
        'elapsedS': round(elapsed, 3),
        'newClips': added,
        'newClipsUnknown': unknown,
        'reelsBefore': _render_reel_counts(before),
        'reelsAfter': _render_reel_counts(after),
        'destinationUnsupported': scan['unsupported'],
    }
"""
)
