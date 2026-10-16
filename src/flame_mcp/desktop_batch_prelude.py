"""Prelude source for `desktop.copy` of a Batch group (`ops/desktop.py`).

Like `desktop_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError` and the
`_dc_*` / `_dm_*` helpers already defined; `desktop_prelude` hands over to `_dcb_snapshot` /
`_dcb_copy` as soon as a source is a PyBatch, and to `_dcb_state*` when it reverts a copy whose
record says `type: 'PyBatch'`.

What it encodes (measured on Flame 2025.2.4, twice, same values):
  - `flame.media_panel.copy(PyBatch, library or library folder)` answers a list with one new PyBatch
    (a new uid, the same name); the library / folder lists it in `children` (a library also in
    `batch_groups`). The source group (uid, nodes, current) is untouched. Copying the current group
    and a group that is not current give the same answer.
  - `flame.media_panel.copy(PyBatch, a library reel)` does not make a PyBatch: the group's schematic
    reel clips (PyClip) go into the reel. So a reel destination is refused before anything is
    written (`INVALID_ARGS`; the Desktop's reels are refused the same way).
  - a PyBatch in a library reads `nodes` as `[]` (its `reels` / `shelf_reels` and their clips read
    fine), has no wiretap id (`get_wiretap_node_id` is None) and `start_frame` / `duration` raise.
    So it is addressed by a name path from its library (when every name on the way is unique) or by
    the Media Panel selection, never by wiretap id, and what a revert compares is its name and its
    reels' / shelf reels' clip names.
  - `flame.media_panel.copy(library PyBatch, desktop)` answers a list with one new PyBatch on the
    Desktop (a new uid, the same name even though the group it came from still exists). Its nodes
    (names, types, connections), schematic reels (clip names, in order) and shelf reels equal the
    original's. The current Batch group does not move.
  - `flame.delete(PyBatch, confirm=False)` answers True for a PyBatch in a library, one on the
    Desktop and the current one. Deleting the current group makes another existing group current.
  - not measured, so refused: several Batch groups in one call, a Batch group together with clips,
    a Desktop group onto the Desktop, a library group into a library or a folder.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope. Messages that start with
`Batch group copy:` make the server add the Batch group hint to the refusal.
"""

from __future__ import annotations

BATCH_COPY_HELPER = """
_DCB_DEST_TYPES = ('PyLibrary', 'PyFolder', 'PyDesktop')


def _dcb_on_desktop(uid):
    for g in list(_dc_desktop().batch_groups):
        if _try(lambda g=g: _v(g.uid)) == uid:
            return True
    return False


def _dcb_container_batches(container):
    if type(container).__name__ == 'PyDesktop':
        return list(container.batch_groups)
    return [c for c in list(container.children) if type(c).__name__ == 'PyBatch']


def _dcb_names_of(objs):
    return [_try(lambda o=o: _v(o.name)) for o in objs]


def _dcb_library_selector(obj):
    # a name path from the library down to a PyBatch inside it; None when the group has no readable
    # chain of parents or a name on the way is not unique (the path would not find it again)
    names = []
    cur = obj
    for _ in range(12):
        name = _try(lambda c=cur: _v(c.name))
        if name is None:
            return None
        names.insert(0, name)
        if type(cur).__name__ == 'PyLibrary':
            break
        cur = _try(lambda c=cur: c.parent)
        if cur is None or type(cur).__name__ not in ('PyLibrary', 'PyFolder', 'PyReel'):
            return None
    else:
        return None
    pool = list(_dc_workspace().libraries)
    for i, name in enumerate(names):
        matches = [c for c in pool if _try(lambda c=c: _v(c.name)) == name]
        if len(matches) != 1:
            return None
        pool = _try(lambda m=matches[0]: list(m.children)) or []
    return {'kind': 'path', 'path': names}


def _dcb_reel_rows(batch, attr):
    return [
        {
            'name': _try(lambda r=r: _v(r.name)),
            'clips': _try(lambda r=r: _entry_names(r)),
        }
        for r in list(getattr(batch, attr))
    ]


def _dcb_state(b):
    uid = _try(lambda: _v(b.uid))
    on_desktop = uid is not None and _dcb_on_desktop(uid)
    state = {
        'name': _try(lambda: _v(b.name)),
        'inLibrary': not on_desktop,
        'reels': _try(lambda: _dcb_reel_rows(b, 'reels')),
        'shelfReels': _try(lambda: _dcb_reel_rows(b, 'shelf_reels')),
    }
    if on_desktop:
        fp = _try(lambda: _batch_fingerprint(b, False))
        state['nodeCount'] = None if fp is None else fp['nodeCount']
        state['fingerprint'] = None if fp is None else fp['fingerprint']
        fa = _try(lambda: _batch_fingerprint(b, False, ['attributes']))
        state['attrFingerprint'] = (
            None if fa is None or fa['unreadableSections'] else fa['fingerprint']
        )
    return state


_DCB_LABELS = (
    ('name', 'name'),
    ('inLibrary', 'location (in a library or on the Desktop)'),
    ('reels', 'reels'),
    ('shelfReels', 'shelf reels'),
    ('nodeCount', 'node count'),
    ('fingerprint', 'node fingerprint (names, types, connections)'),
    ('attrFingerprint', 'node attribute values'),
)


def _dcb_has_none(value):
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return any(_dcb_has_none(v) for v in value)
    if isinstance(value, dict):
        return any(_dcb_has_none(v) for v in value.values())
    return False


def _dcb_state_problems(baseline, current):
    # what a revert compares before it deletes a Batch group the server made. A group in a library
    # reads no nodes, so there its name and its reels' / shelf reels' clip names are all there is;
    # on the Desktop the node fingerprint and the node attribute values are compared as well. A
    # value that could not be read (None, in the record or now) is never a match: it is a problem,
    # and so is a record that lacks a fingerprint to compare with
    if not baseline:
        return [
            'no state of it was recorded when it was made, so it cannot be told whether it was '
            'changed since'
        ]
    problems = []
    if baseline.get('inLibrary') is False:
        for key, label in (
            ('fingerprint', 'node fingerprint'),
            ('attrFingerprint', 'node attribute fingerprint'),
        ):
            if key not in baseline:
                problems.append(
                    'no ' + label + ' of it was recorded when it was made, so it cannot be told '
                    'whether it was edited since'
                )
    for key, label in _DCB_LABELS:
        if key not in baseline:
            continue
        was, now = baseline[key], current.get(key)
        if _dcb_has_none(was) or _dcb_has_none(now):
            problems.append(
                label + ' could not be read (it was ' + repr(was) + ', it is ' + repr(now)
                + '): it cannot be told whether the Batch group changed'
            )
        elif was != now:
            problems.append(
                label + ' is ' + repr(now) + ' now, it was ' + repr(was)
                + ' when the Batch group was made'
            )
    return problems


def _dcb_check_dest(dest, source):
    # source: the one PyBatch, or None (a journal.check read, which has no sources)
    name = type(dest).__name__
    if name == 'PyReel':
        raise _OpError(
            'INVALID_ARGS',
            'Batch group copy: destination is a reel (' + repr(_try(lambda: _v(dest.name)))
            + '): Flame puts the clips of the group into a reel, not the group. Use a library, a '
            'library folder or (for a Batch group in a library) the Desktop',
        )
    if name not in _DCB_DEST_TYPES:
        raise _OpError(
            'SELECTOR_TYPE',
            'destination is a ' + name + ', not a library, a library folder or the Desktop',
        )
    if name != 'PyDesktop':
        closed = _dc_library_problem(dest)
        if closed is not None:
            raise _OpError('PRECONDITION_UNMET', 'Batch group copy: ' + closed)
    if source is None:
        return
    from_desktop = _dcb_on_desktop(_try(lambda: _v(source.uid)))
    if not from_desktop:
        closed = _dc_library_problem(_try(lambda: source.parent))
        if closed is not None:
            raise _OpError('PRECONDITION_UNMET', 'Batch group copy: ' + closed)
    if name == 'PyDesktop' and from_desktop:
        raise _OpError(
            'INVALID_ARGS',
            'Batch group copy: a Batch group of the Desktop cannot be copied onto the Desktop; '
            'copy it into a library, and a library copy onto the Desktop',
        )
    if name != 'PyDesktop' and not from_desktop:
        raise _OpError(
            'INVALID_ARGS',
            'Batch group copy: a Batch group inside a library is copied onto the Desktop '
            '(destination {kind: desktop}), not into a library or a folder',
        )


def _dcb_snapshot(dest, sources, known=None):
    for i, c in enumerate(sources):
        if type(c).__name__ != 'PyBatch':
            raise _OpError(
                'INVALID_ARGS',
                'Batch group copy: sources[' + str(i) + '] is a ' + type(c).__name__
                + ': one Batch group is copied alone, not together with clips or sequences',
            )
    if len(sources) > 1:
        raise _OpError(
            'INVALID_ARGS',
            'Batch group copy: one Batch group per call, got ' + str(len(sources)),
        )
    _dcb_check_dest(dest, sources[0] if sources else None)
    entries = _dcb_container_batches(dest)
    existing = _dc_rows(entries)
    for row in existing:
        row['type'] = 'PyBatch'
    if known is not None:
        for row, entry in zip(existing, entries):
            if row['uid'] not in known:
                row['state'] = _dcb_state(entry)
    return {
        'destination': _dc_with_selector(dest),
        'existing': existing,
        'sources': [_dc_with_selector(c) for c in sources],
        'error': None,
        'errorCode': None,
    }


def _dcb_describe(out, dest, src_uid, src_container, copies, existing_uids, current_before):
    for c in copies:
        row = dict(_dc_with_selector(c), sourceUid=src_uid)
        row['state'] = _dcb_state(c)
        if row['uid'] is None or row['uid'] == src_uid or row['uid'] in existing_uids:
            out['notNew'].append(row)
        else:
            out['copies'].append(row)
    after = _dcb_container_batches(dest)
    after_uids = [_try(lambda o=o: _v(o.uid)) for o in after]
    out['destinationEntriesAfter'] = _dcb_names_of(after)
    for row in out['copies']:
        row['inDestination'] = row['uid'] in after_uids
    if src_container is not None:
        kept = [_try(lambda o=o: _v(o.uid)) for o in _dcb_container_batches(src_container)]
        out['sourcesRemain'] = src_uid in kept
    if out['sourcesRemain'] is False:
        out['warnings'].append(
            'the source group is not found by its uid after the copy: the call moved it or '
            'deleted it'
        )
    if len(out['copies']) == 1:
        out['copyNodeCount'] = out['copies'][0]['state'].get('nodeCount')
        out['copyFingerprint'] = out['copies'][0]['state'].get('fingerprint')
    out['current'] = _dc_current()
    out['currentMoved'] = (out['current'] or {}).get('uid') != (current_before or {}).get('uid')
    if out['currentMoved']:
        out['warnings'].append('the current Batch group changed during the copy')
    wrong = [type(c).__name__ for c in copies if type(c).__name__ != 'PyBatch']
    if len(copies) != 1:
        out['error'] = 'copy returned ' + str(len(copies)) + ' object(s) for 1 Batch group'
        out['errorCode'] = 'SILENT_NOOP'
    elif wrong:
        out['error'] = 'copy returned a ' + wrong[0] + ', not a PyBatch'
        out['errorCode'] = 'SILENT_NOOP'
    elif out['notNew']:
        out['error'] = 'copy returned a group that is not new (the source, or one that was there)'
        out['errorCode'] = 'SILENT_NOOP'
    elif not all(r['inDestination'] for r in out['copies']):
        out['error'] = (
            'copy returned without an error but the destination does not list the new group '
            '(had ' + repr(out['destinationEntriesBefore']) + ', now '
            + repr(out['destinationEntriesAfter']) + ')'
        )
        out['errorCode'] = 'SILENT_NOOP'


def _dcb_copy(dest, sources, expected):
    snapshot = _dcb_snapshot(dest, sources)
    src = sources[0]
    src_uid = snapshot['sources'][0]['uid']
    src_container = _dc_desktop() if _dcb_on_desktop(src_uid) else _try(lambda: src.parent)
    existing = _dcb_container_batches(dest)
    existing_uids = [_try(lambda o=o: _v(o.uid)) for o in existing]
    current_before = _dc_current()
    src_state = _dcb_state(src)
    out = {
        'destination': snapshot['destination'],
        'requested': 1,
        'returned': 0,
        'copies': [],
        'notNew': [],
        'sources': snapshot['sources'],
        'sourcesRemain': None,
        'destinationEntriesBefore': _dcb_names_of(existing),
        'destinationEntriesAfter': None,
        'sourceNodeCount': src_state.get('nodeCount'),
        'sourceFingerprint': src_state.get('fingerprint'),
        'copyNodeCount': None,
        'copyFingerprint': None,
        'currentBefore': current_before,
        'current': None,
        'currentMoved': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if expected is not None and sorted(existing_uids, key=str) != sorted(expected, key=str):
        out['error'] = 'nothing was copied: the destination changed between the read and the write'
        out['errorCode'] = 'CONFLICT'
        return out
    returned = flame.media_panel.copy(src, dest, duplicate_action='add')
    copies = list(returned) if isinstance(returned, (list, tuple)) else []
    out['returned'] = len(copies)
    try:
        _dcb_describe(out, dest, src_uid, src_container, copies, existing_uids, current_before)
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the copy was made but reading it back failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out
"""
