"""Prelude source for the `batch` category's own ops (`ops/batch.py`).

Like `checkpoint_prelude` and `attribute_prelude`: real Python source that
`execution.build_exec_code` places ahead of an op's own code, so each op's `to_python` output is
a few loop-free lines (registry.py's pacing guard parses only that output). It runs inside Flame
(3.11) with `flame`, `_v`, `_j`, `_ja` and `_OpError` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `PyBatch.reels` (schematic reels) and `PyBatch.shelf_reels`; `create_reel` /
    `create_shelf_reel` return the PyReel and add it to the matching list. A reel's `type` is
    'Schematic' or 'Shelf'.
  - `flame.media_panel.copy(clip(s), reel)`: copying a clip (a list of them for several) into a
    schematic reel also creates a Clip node in the group; into a shelf reel it does not.
  - `PyBatch.iterate(index=-1)`: with no index the next free one (highest + 1, starting at 1);
    with the index of an existing iteration it overwrites that iteration without warning. The
    iteration's number is `PyBatchIteration.iteration_number`.
  - `PyBatch.organize()` moves nodes (positions only) and returns True.
  - `PyBatch.save_setup(path)` rewrites the path (a `.batch` is appended, dots become
    underscores, a directory of the same stem is written beside it): the file is found by
    comparing the directory's `.batch` files before and after.
  - a write that raises nothing but leaves no trace is an error (`SILENT_NOOP`): every write is
    read back. The op's `_result` is kept beside such an error (execution.py).
"""

from __future__ import annotations

BATCH_HELPER = """
def _try(fn):
    try:
        return fn()
    except BaseException:
        return None


def _reel_names(objs):
    return [_try(lambda r=r: _v(r.name)) for r in objs]


def _clip_names(reel):
    try:
        return [_v(c.name) for c in list(reel.clips)]
    except BaseException:
        return None


def _sequence_names(reel):
    try:
        return [_v(c.name) for c in list(reel.sequences)]
    except BaseException:
        return None


def _entry_names(reel):
    # clips and sequences: a sequence copied into a reel lands in `reel.sequences`, not in
    # `reel.clips`
    clips, sequences = _clip_names(reel), _sequence_names(reel)
    if clips is None or sequences is None:
        return None
    return clips + sequences


def _reel_rows(batch, attr, limit=100):
    rows = []
    for r in list(getattr(batch, attr)):
        clips = _clip_names(r)
        sequences = _sequence_names(r)
        rows.append(
            {
                'name': _try(lambda r=r: _v(r.name)),
                'type': _try(lambda r=r: _ja(r.type)),
                'uid': _try(lambda r=r: _v(r.uid)),
                'clipCount': None if clips is None else len(clips),
                'clips': None if clips is None else clips[:limit],
                'clipsTruncated': clips is not None and len(clips) > limit,
                'sequenceCount': None if sequences is None else len(sequences),
                'sequences': None if sequences is None else sequences[:limit],
            }
        )
    return rows


def _iteration_rows(batch):
    rows = []
    for it in list(batch.batch_iterations):
        rows.append(
            {
                'name': _try(lambda it=it: _v(it.name)),
                'number': _try(lambda it=it: _ja(it.iteration_number)),
                'uid': _try(lambda it=it: _v(it.uid)),
            }
        )
    rows.sort(key=lambda e: (e['number'] is None, e['number'] if e['number'] is not None else 0))
    return rows


def _current_batch_uid():
    desktop = flame.projects.current_project.current_workspace.desktop
    current = _v(desktop.current_batch_group)
    return None if current is None else _v(current.uid)


def _batch_info(batch):
    uid = _try(lambda: _v(batch.uid))
    current_uid = _try(_current_batch_uid)
    unreadable = []

    def field(name, fn):
        try:
            return fn()
        except BaseException as exc:
            unreadable.append(name + ': ' + (str(exc) or type(exc).__name__))
            return None

    return {
        'name': field('name', lambda: _v(batch.name)),
        'uid': uid,
        'isCurrent': None if current_uid is None or uid is None else current_uid == uid,
        'duration': field('duration', lambda: _ja(batch.duration)),
        'startFrame': field('start_frame', lambda: _ja(batch.start_frame)),
        'nodeCount': field('nodes', lambda: len(list(batch.nodes))),
        'reels': field('reels', lambda: _reel_rows(batch, 'reels')),
        'shelfReels': field('shelf_reels', lambda: _reel_rows(batch, 'shelf_reels')),
        'iterations': field('batch_iterations', lambda: _iteration_rows(batch)),
        'currentIterationNumber': field(
            'current_iteration_number', lambda: _ja(batch.current_iteration_number)
        ),
        'unreadable': unreadable,
    }


def _create_reel(batch, name, shelf):
    attr = 'shelf_reels' if shelf else 'reels'
    before = list(getattr(batch, attr))
    before_uids = [_try(lambda r=r: _v(r.uid)) for r in before]
    before_names = _reel_names(before)
    reel = batch.create_shelf_reel(name) if shelf else batch.create_reel(name)
    after = list(getattr(batch, attr))
    after_uids = [_try(lambda r=r: _v(r.uid)) for r in after]
    new = [r for r, u in zip(after, after_uids) if u is None or u not in before_uids]
    found = None
    returned_uid = _try(lambda: _v(reel.uid))
    if returned_uid is not None and returned_uid in after_uids:
        found = reel
    elif len(new) == 1:
        found = new[0]
    error = None
    if found is None:
        error = (
            'the call returned without an error but no new reel appeared in '
            + attr
            + ' (had '
            + repr(before_names)
            + ', now '
            + repr(_reel_names(after))
            + ')'
        )
    return {
        'kind': 'shelf' if shelf else 'schematic',
        'requestedName': name,
        'reel': None
        if found is None
        else {
            'name': _try(lambda: _v(found.name)),
            'type': _try(lambda: _ja(found.type)),
            'uid': _try(lambda: _v(found.uid)),
            'pyType': type(found).__name__,
        },
        'renamed': found is not None and _try(lambda: _v(found.name)) != name,
        'reelsBefore': before_names,
        'reelsAfter': _reel_names(after),
        'error': error,
    }


def _find_reel(batch, name, kind):
    found = []
    for attr, label in (('reels', 'schematic'), ('shelf_reels', 'shelf')):
        if kind is not None and kind != label:
            continue
        for r in list(getattr(batch, attr)):
            if _try(lambda r=r: _v(r.name)) == name:
                found.append((r, label))
    if len(found) == 0:
        raise _OpError('SELECTOR_EMPTY', 'no ' + (kind or '') + ' reel named ' + repr(name))
    if len(found) > 1:
        raise _OpError(
            'SELECTOR_AMBIGUOUS',
            'ambiguous: ' + str(len(found)) + ' reels named ' + repr(name)
            + ' (pass reel_kind to choose)',
        )
    return found[0]


def _add_clips(batch, reel_name, reel_kind, clips):
    reel, label = _find_reel(batch, reel_name, reel_kind)
    for i, c in enumerate(clips):
        if type(c).__name__ not in ('PyClip', 'PySequence'):
            raise _OpError(
                'SELECTOR_TYPE',
                'clips[' + str(i) + '] is a ' + type(c).__name__ + ', not a PyClip / PySequence',
            )
    nodes_before = [_v(n.name) for n in list(batch.nodes)]
    clips_before = _entry_names(reel)
    returned = flame.media_panel.copy(clips[0] if len(clips) == 1 else list(clips), reel)
    nodes_after = [_v(n.name) for n in list(batch.nodes)]
    clips_after = _entry_names(reel)
    copied = []
    if isinstance(returned, (list, tuple)):
        copied = [_try(lambda c=c: _v(c.name)) for c in returned]
    remaining = list(nodes_before)
    new_nodes = []
    for n in nodes_after:
        if n in remaining:
            remaining.remove(n)
        else:
            new_nodes.append(n)
    added = None
    if clips_before is not None and clips_after is not None:
        added = len(clips_after) - len(clips_before)
    error = None
    if added != len(clips):
        error = (
            'copy returned without an error but the reel gained '
            + repr(added)
            + ' clip(s) / sequence(s), expected '
            + str(len(clips))
        )
    expected_nodes = len(clips) if label == 'schematic' else 0
    return {
        'reel': {'name': reel_name, 'kind': label},
        'revertCovers': label == 'schematic',
        'requested': len(clips),
        'returned': len(copied),
        'copied': copied,
        'reelClipsBefore': clips_before,
        'reelClipsAfter': clips_after,
        'clipNodesAdded': new_nodes,
        'expectedClipNodes': expected_nodes,
        'clipNodesAsExpected': len(new_nodes) == expected_nodes,
        'nodesBefore': len(nodes_before),
        'nodesAfter': len(nodes_after),
        'error': error,
    }


def _positions(batch):
    out = {}
    for n in list(batch.nodes):
        out[_try(lambda n=n: _v(n.name))] = [
            _try(lambda n=n: _ja(n.pos_x)),
            _try(lambda n=n: _ja(n.pos_y)),
        ]
    return out


def _organize(batch):
    before = _positions(batch)
    returned = batch.organize()
    after = _positions(batch)
    moved = [k for k in after if k in before and before[k] != after[k]]
    error = None
    if returned is not True:
        error = 'organize returned ' + repr(_ja(returned)) + ' (expected True)'
    return {
        'returned': _ja(returned),
        'nodeCount': len(after),
        'moved': len(moved),
        'movedNodes': moved[:50],
        'positionsBefore': before,
        'positionsAfter': after,
        'error': error,
    }


def _iterate(batch, index, overwrite):
    before = _iteration_rows(batch)
    numbers = [row['number'] for row in before]
    overwrites = index is not None and index in numbers
    if overwrites and not overwrite:
        raise _OpError(
            'INVALID_ARGS',
            'iteration ' + str(index) + ' already exists: iterate(index=) would overwrite it '
            'without warning. Pass overwrite=true to do that.',
        )
    created = batch.iterate() if index is None else batch.iterate(index=index)
    after = _iteration_rows(batch)
    number = _try(lambda: _ja(created.iteration_number))
    error = None
    if created is None or number is None:
        error = 'iterate returned ' + repr(type(created).__name__) + ' without a number'
    elif index is not None and number != index:
        error = 'iterate(index=' + str(index) + ') made iteration ' + repr(number)
    elif number not in [row['number'] for row in after]:
        error = 'iteration ' + repr(number) + ' is not in batch_iterations afterwards'
    return {
        'iteration': {
            'name': _try(lambda: _v(created.name)),
            'number': number,
            'uid': _try(lambda: _v(created.uid)),
        },
        'requestedIndex': index,
        'overwrote': overwrites,
        'iterationsBefore': before,
        'iterationsAfter': after,
        'currentIterationNumber': _try(lambda: _ja(batch.current_iteration_number)),
        'error': error,
    }


def _setup_files(directory):
    import glob
    import os
    out = {}
    for p in glob.glob(os.path.join(glob.escape(directory), '*.batch')):
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return out


def _save_setup(batch, path, overwrite):
    import os
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _OpError('INVALID_ARGS', 'path must be an absolute path, got ' + repr(path))
    directory, name = os.path.split(path)
    if not name:
        raise _OpError('INVALID_ARGS', 'path names a directory, not a file: ' + repr(path))
    if not os.path.isdir(directory):
        raise _OpError('PRECONDITION_UNMET', 'the directory does not exist: ' + repr(directory))
    predicted = [os.path.join(directory, name.replace('.', '_') + '.batch')]
    if name.endswith('.batch'):
        predicted.append(path)
    existing = [p for p in predicted if os.path.exists(p)]
    if existing and not overwrite:
        raise _OpError(
            'PRECONDITION_UNMET',
            'save_setup would overwrite ' + repr(existing) + '; pass overwrite=true to do that',
        )
    before = _setup_files(directory)
    returned = batch.save_setup(path)
    after = _setup_files(directory)
    written = sorted(p for p in after if p not in before or after[p] != before[p])
    overwritten = [p for p in written if p in before]
    error = None
    if returned is not True:
        error = 'save_setup returned ' + repr(_ja(returned)) + ' (expected True)'
    elif len(written) != 1:
        error = (
            'save_setup wrote no single .batch file in '
            + repr(directory)
            + '; new or changed: '
            + repr(written)
        )
    return {
        'requestedPath': path,
        'path': written[0] if len(written) == 1 else None,
        'size': os.path.getsize(written[0]) if len(written) == 1 else None,
        'written': written,
        'overwritten': overwritten,
        'saveSetupReturned': _ja(returned),
        'batch': {'uid': _try(lambda: _v(batch.uid)), 'name': _try(lambda: _v(batch.name))},
        'error': error,
    }
"""
