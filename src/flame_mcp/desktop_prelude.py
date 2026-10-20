"""Prelude source for `desktop.create` and `desktop.copy` (`ops/desktop.py`).

Like `batch_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code, so each op's `to_python` output is a few loop-free lines (registry.py's pacing guard
parses only that output). It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try` and
`_OpError` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `PyDesktop.create_batch_group(name, nb_reels, nb_shelf_reels, reels, shelf_reels, start_frame,
    duration)` returns a PyBatch and always makes it the current Batch group; a name that is taken
    gets " 1" appended. `reels` given = those reels (it wins over nb_reels); `shelf_reels` given =
    those shelf reels and no `Batch Renders`. No arguments: Flame's default schematic reels (the
    names are those of the Flame version: `result.reels` is the actual list), one shelf
    reel `Batch Renders`, start_frame 1, duration 100. A new group holds no node.
  - `PyDesktop.create_reel_group(name)` returns a PyReelGroup with `Reel 1` to `4` and `Sequences`
    (type `Sequences`); a name that is taken becomes the next number (`~RG1` -> `~RG2`).
  - `PyReelGroup.create_reel(name, sequence=False)` returns a PyReel at the end of the group (type
    `Reel`, or `Sequences` with sequence=True); a taken name becomes the next number too.
    `PyLibrary.create_reel(name)` exists (measured with a name only).
  - `flame.media_panel.copy(clip or [clips], destination, duplicate_action='add')` returns the
    copies (new uids, the sources stay). `duplicate_action` is `add` or `replace` (anything else
    is a RuntimeError); `replace` did not replace anything, so only `add` is ever passed.
  - `flame.delete(obj, confirm=False)` returns True for a Batch group (Clip nodes and all), a reel
    group, a reel, a clip; afterwards `flame.find_by_uid(uid)` is None. Deleting the current Batch
    group makes another existing group current (not the one that was current before it).
    Deleting the last reel of a reel group makes Flame create the default reels again.
  - `PyWorkspace.create_library(name)` returns a PyLibrary at the end of `libraries`; a taken name
    becomes `<name> 1` (a new uid, no exception). An empty library is deleted with
    `flame.delete(lib, confirm=False)`. `library.create` runs through `kind='library'`.
  - a write that raises nothing but leaves no trace is an error: every write is read back. The
    op's `_result` is kept beside such an error (execution.py).

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

DESKTOP_HELPER = """
_DC_TYPES = {
    'batch_group': 'PyBatch',
    'reel_group': 'PyReelGroup',
    'reel': 'PyReel',
    'library': 'PyLibrary',
}
_DC_ENTRY_TYPES = ('PyClip', 'PySequence')
_DC_REEL_PARENTS = ('PyReelGroup', 'PyLibrary')
_DC_DEST_TYPES = ('PyReel', 'PyLibrary', 'PyFolder')


def _dc_workspace():
    return flame.projects.current_project.current_workspace


def _dc_desktop():
    return _dc_workspace().desktop


def _dc_library_problem(obj):
    # None when `obj` (a library, a folder or a reel of a library) is not in a library or its
    # library is open; else why nothing may be done with it. A closed library may list nothing at
    # all, so what it holds is never judged while it is closed or while whether it is open cannot
    # be read
    cur = obj
    for _ in range(12):
        kind = type(cur).__name__
        if kind == 'PyLibrary':
            name = _try(lambda c=cur: _v(c.name))
            opened = _try(lambda c=cur: _v(c.opened))
            if opened is True:
                return None
            if opened is False:
                return 'the library ' + repr(name) + ' is closed: open it first'
            return 'whether the library ' + repr(name) + ' is open could not be read'
        if kind not in ('PyFolder', 'PyReel'):
            return None
        cur = _try(lambda c=cur: c.parent)
        if cur is None:
            return (
                'the library that holds it could not be found, so whether it is open is not '
                'known'
            )
    return 'the library that holds it could not be found, so whether it is open is not known'


def _dc_entry(obj):
    return {
        'type': type(obj).__name__,
        'name': _try(lambda: _v(obj.name)),
        'uid': _try(lambda: _v(obj.uid)),
        'wiretapId': _try(lambda: obj.get_wiretap_node_id()),
    }


def _dc_selector(entry):
    if entry['type'] == 'PyDesktop':
        return {'kind': 'desktop'}
    if entry['type'] == 'PyBatch':
        return {'kind': 'batch', 'uid': entry['uid']} if entry['uid'] else None
    if entry['wiretapId']:
        return {'kind': 'wiretap', 'id': entry['wiretapId']}
    return None


def _dc_with_selector(obj):
    entry = _dc_entry(obj)
    entry['selector'] = _dc_selector(entry)
    if entry['type'] == 'PyBatch' and entry['uid'] and not _dcb_on_desktop(entry['uid']):
        entry['selector'] = _dcb_library_selector(obj)
    return entry


def _dc_current():
    try:
        cur = _v(_dc_desktop().current_batch_group)
        if cur is None:
            return None
        return {'name': _v(cur.name), 'uid': _v(cur.uid)}
    except BaseException as exc:
        return {'name': None, 'uid': None, 'unreadable': type(exc).__name__ + ': ' + str(exc)}


def _dc_entry_count(reel):
    total = 0
    for attr in ('clips', 'sequences'):
        try:
            total += len(list(getattr(reel, attr)))
        except BaseException:
            return None
    return total


def _dc_children(kind, parent):
    if kind == 'batch_group':
        return list(_dc_desktop().batch_groups)
    if kind == 'reel_group':
        return list(_dc_desktop().reel_groups)
    if kind == 'library':
        return list(_dc_workspace().libraries)
    return list(parent.reels)


def _dc_rows(objs):
    return [
        {'name': _try(lambda o=o: _v(o.name)), 'uid': _try(lambda o=o: _v(o.uid))} for o in objs
    ]


def _dc_names(objs):
    return [_try(lambda o=o: _v(o.name)) for o in objs]


def _dc_check_parent(kind, parent):
    if kind != 'reel':
        return
    name = type(parent).__name__
    if name not in _DC_REEL_PARENTS:
        raise _OpError(
            'SELECTOR_TYPE',
            'parent is a ' + name + ', not a reel group of the Desktop or a library',
        )


def _dc_snapshot(kind, parent, sequence):
    _dc_check_parent(kind, parent)
    if sequence and type(parent).__name__ != 'PyReelGroup':
        raise _OpError(
            'INVALID_ARGS',
            'sequence=true is accepted only for the reel of a reel group; a library reel is '
            'made with a name only',
        )
    kids = _dc_children(kind, parent)
    return {
        'kind': kind,
        'parent': None if parent is None else _dc_with_selector(parent),
        'existing': _dc_rows(kids),
        'current': _dc_current() if kind == 'batch_group' else None,
        'error': None,
        'errorCode': None,
    }


def _dc_reel_rows(objs):
    rows = []
    for r in objs:
        entry = _dc_with_selector(r)
        entry['type'] = _try(lambda r=r: _ja(r.type))
        rows.append(entry)
    return rows


def _dc_state(kind, obj):
    if kind == 'batch_group':
        return {
            'nodeCount': len(list(obj.nodes)),
            'reels': [
                {'name': _try(lambda r=r: _v(r.name)), 'entries': _dc_entry_count(r)}
                for r in list(obj.reels)
            ],
            'shelfReels': [
                {'name': _try(lambda r=r: _v(r.name)), 'entries': _dc_entry_count(r)}
                for r in list(obj.shelf_reels)
            ],
        }
    if kind == 'reel_group':
        return {
            'reels': [
                {
                    'name': _try(lambda r=r: _v(r.name)),
                    'uid': _try(lambda r=r: _v(r.uid)),
                    'entries': _dc_entry_count(r),
                }
                for r in list(obj.reels)
            ]
        }
    if kind == 'library':
        return {'entries': len(list(obj.children))}
    return {'entries': _dc_entry_count(obj)}


def _dc_batch_facts(bg):
    return {
        'reels': _dc_names(list(bg.reels)),
        'shelfReels': _dc_names(list(bg.shelf_reels)),
        'startFrame': _try(lambda: _ja(bg.start_frame)),
        'duration': _try(lambda: _ja(bg.duration)),
        'nodeCount': len(list(bg.nodes)),
    }


def _dc_call_create(kind, parent, name, opts):
    if kind == 'batch_group':
        kwargs = {}
        for key in ('reels', 'shelf_reels', 'start_frame', 'duration'):
            if opts.get(key) is not None:
                kwargs[key] = opts[key]
        return _dc_desktop().create_batch_group(name, **kwargs)
    if kind == 'reel_group':
        return _dc_desktop().create_reel_group(name)
    if kind == 'library':
        return _dc_workspace().create_library(name)
    if opts.get('sequence'):
        return parent.create_reel(name, sequence=True)
    return parent.create_reel(name)


def _dc_mismatches(kind, facts, opts):
    out = []
    if kind == 'batch_group':
        for key, want in (
            ('reels', opts.get('reels')),
            ('shelfReels', opts.get('shelf_reels')),
            ('startFrame', opts.get('start_frame')),
            ('duration', opts.get('duration')),
        ):
            if want is not None and facts.get(key) != want:
                out.append(key + ' asked for ' + repr(want) + ', read back ' + repr(facts.get(key)))
    return out


def _dc_describe_created(out, kind, parent, made, uids, opts):
    entry = _dc_with_selector(made)
    after_uids = [_try(lambda o=o: _v(o.uid)) for o in _dc_children(kind, parent)]
    out['returned'] = entry
    if type(made).__name__ != _DC_TYPES[kind]:
        out['error'] = 'the call returned a ' + type(made).__name__ + ', not a ' + _DC_TYPES[kind]
        out['errorCode'] = 'SILENT_NOOP'
        return
    if entry['uid'] is None or entry['uid'] not in after_uids or entry['uid'] in uids:
        out['error'] = (
            'the call returned without an error but its ' + kind + ' is not a new entry of the '
            'list (uid ' + repr(entry['uid']) + ')'
        )
        out['errorCode'] = 'SILENT_NOOP'
        return
    out['created'] = entry
    out['uid'] = entry['uid']
    out['selector'] = entry['selector']
    out['name'] = entry['name']
    out['renamed'] = entry['name'] != out['requestedName']
    out['state'] = _dc_state(kind, made)
    if kind == 'batch_group':
        out.update(_dc_batch_facts(made))
        out['current'] = _dc_current()
        out['currentMoved'] = out['current'] is not None and (
            out['current'].get('uid') == entry['uid']
        )
        out['warnings'] = _dc_mismatches(kind, out, opts)
    elif kind == 'reel_group':
        out['reelRows'] = _dc_reel_rows(list(made.reels))
    elif kind == 'library':
        pass
    else:
        out['type'] = _try(lambda: _ja(made.type))
        out['sequence'] = bool(opts.get('sequence'))
        if type(parent).__name__ == 'PyReelGroup':
            want = 'Sequences' if opts.get('sequence') else 'Reel'
            if out['type'] != want:
                out['warnings'].append('type ' + repr(out['type']) + ', expected ' + repr(want))


def _dc_create(kind, parent, name, allow_rename, opts, expected):
    _dc_check_parent(kind, parent)
    before = _dc_children(kind, parent)
    names = _dc_names(before)
    uids = [_try(lambda o=o: _v(o.uid)) for o in before]
    out = {
        'kind': kind,
        'requestedName': name,
        'existingNames': names,
        'created': None,
        'state': None,
        'parent': None if parent is None else _dc_with_selector(parent),
        'previousCurrent': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if expected is not None and sorted(uids, key=str) != sorted(expected, key=str):
        out['error'] = (
            'nothing was created: the list of ' + kind + 's changed between the read and the write'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    if name in names and not allow_rename:
        out['error'] = 'nothing was created: ' + repr(name) + ' already exists'
        out['errorCode'] = 'CONFLICT'
        return out
    if kind == 'batch_group':
        out['previousCurrent'] = _dc_current()
    made = _dc_call_create(kind, parent, name, opts)
    try:
        _dc_describe_created(out, kind, parent, made, uids, opts)
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the ' + kind + ' was made but reading it back failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _dc_scan(pool, uid):
    for obj in pool:
        if _try(lambda o=obj: _v(o.uid)) == uid:
            return obj
    return None


def _dc_find_by_uid(uid):
    try:
        return flame.find_by_uid(uid)
    except BaseException:
        return None


def _dc_find_parent(entry):
    if not isinstance(entry, dict) or not entry.get('uid'):
        return None
    if entry.get('type') == 'PyReelGroup':
        return _dc_scan(list(_dc_desktop().reel_groups), entry['uid'])
    if entry.get('type') == 'PyLibrary':
        return _dc_scan(list(_dc_workspace().libraries), entry['uid'])
    if entry.get('type') == 'PyDesktop':
        return _dc_desktop()
    return _dc_find_by_uid(entry['uid'])


def _dc_find_created(kind, uid, parent_entry=None):
    if kind == 'batch_group':
        return _dc_scan(list(_dc_desktop().batch_groups), uid)
    if kind == 'reel_group':
        return _dc_scan(list(_dc_desktop().reel_groups), uid)
    if kind == 'library':
        return _dc_scan(list(_dc_workspace().libraries), uid)
    parent = _dc_find_parent(parent_entry)
    if parent is not None:
        found = _dc_scan(list(parent.reels), uid)
        if found is not None:
            return found
    return _dc_find_by_uid(uid)


def _dc_state_problems(kind, created, current):
    problems = []
    baseline = created.get('state')
    if kind == 'batch_group':
        if current['nodeCount'] != 0:
            problems.append('the group holds ' + str(current['nodeCount']) + ' node(s)')
        for label, key in (('reel', 'reels'), ('shelf reel', 'shelfReels')):
            if baseline is not None:
                want = sorted([r['name'] for r in baseline[key]], key=str)
                have = sorted([r['name'] for r in current[key]], key=str)
                if want != have:
                    problems.append(
                        label + 's are ' + repr(have) + ' now, they were ' + repr(want)
                        + ' when the group was created'
                    )
            for r in current[key]:
                if r['entries'] != 0:
                    problems.append(
                        label + ' ' + repr(r['name']) + ' holds ' + repr(r['entries'])
                        + ' clip(s) / sequence(s)'
                    )
    elif kind == 'reel_group':
        for r in current['reels']:
            if r['entries'] != 0:
                problems.append(
                    'reel ' + repr(r['name']) + ' holds ' + repr(r['entries'])
                    + ' clip(s) / sequence(s)'
                )
    elif kind == 'library':
        if current['entries'] != 0:
            problems.append('the library holds ' + repr(current['entries']) + ' entr(ies)')
    elif current['entries'] != 0:
        problems.append('the reel holds ' + repr(current['entries']) + ' clip(s) / sequence(s)')
    return problems


def _dc_revert_created(kind, created):
    uid = created.get('uid')
    out = {
        'kind': kind,
        'uid': uid,
        'name': created.get('name'),
        'deleted': False,
        'alreadyGone': False,
        'conflicts': [],
        'currentState': None,
        'error': None,
        'errorCode': None,
        'previousCurrent': created.get('previousCurrent'),
    }
    obj = _dc_find_created(kind, uid, created.get('parent'))
    if obj is None:
        out['alreadyGone'] = True
        out['current'] = _dc_current() if kind == 'batch_group' else None
        return out
    if type(obj).__name__ != _DC_TYPES[kind]:
        out['error'] = (
            'the object with uid ' + repr(uid) + ' is a ' + type(obj).__name__ + ', not a '
            + _DC_TYPES[kind] + ': nothing was deleted'
        )
        out['errorCode'] = 'SELECTOR_TYPE'
        return out
    if kind == 'library':
        closed = _dc_library_problem(obj)
        if closed is not None:
            out['conflicts'] = [closed]
            out['error'] = 'nothing was deleted: ' + closed
            out['errorCode'] = 'CONFLICT'
            return out
    state = _dc_state(kind, obj)
    out['currentState'] = state
    problems = _dc_state_problems(kind, created, state)
    if problems:
        out['conflicts'] = problems
        out['error'] = (
            'nothing was deleted: what the call made has changed since (' + '; '.join(problems)
            + ')'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    parent = _dc_find_parent(created.get('parent')) if kind == 'reel' else None
    reels_before = _dc_names(list(parent.reels)) if parent is not None else None
    returned = flame.delete(obj, confirm=False)
    still = _dc_find_created(kind, uid, created.get('parent'))
    out['deleteReturned'] = _ja(returned)
    out['deleted'] = returned is True and still is None
    if not out['deleted']:
        out['error'] = (
            'flame.delete returned ' + repr(_ja(returned)) + ' and the ' + kind + ' is '
            + ('still there' if still is not None else 'gone')
        )
        out['errorCode'] = 'SILENT_NOOP'
    if kind == 'batch_group':
        out['current'] = _dc_current()
    if kind == 'reel':
        out['parentReelsBefore'] = reels_before
        parent_after = _dc_find_parent(created.get('parent'))
        out['parentReelsAfter'] = (
            _dc_names(list(parent_after.reels)) if parent_after is not None else None
        )
    return out


def _dc_alive(kind, items):
    found = {}
    for item in items:
        uid = item.get('uid')
        if kind == 'copy':
            found[uid] = _dm_find(uid, item.get('parent')) is not None
        else:
            found[uid] = _dc_find_created(kind, uid, item.get('parent')) is not None
    return {'kind': kind, 'alive': found, 'error': None, 'errorCode': None}


def _dc_dest_entries(dest):
    return list(dest.clips) + list(dest.sequences)


def _dc_is_batch_reel(dest):
    return type(dest).__name__ == 'PyReel' and _try(lambda: _ja(dest.type)) in (
        'Schematic',
        'Shelf',
    )


def _dc_copy_snapshot(dest, sources, known=None, batch=False):
    if batch or any(type(c).__name__ == 'PyBatch' for c in sources):
        return _dcb_snapshot(dest, sources, known)
    name = type(dest).__name__
    if name not in _DC_DEST_TYPES:
        raise _OpError(
            'SELECTOR_TYPE',
            'destination is a ' + name + ', not a reel, a library or a folder',
        )
    if _dc_is_batch_reel(dest):
        raise _OpError(
            'INVALID_ARGS',
            'destination is a reel of a Batch group (type ' + repr(_ja(dest.type)) + '): use '
            'batch.add_clips to put clips into a Batch group',
        )
    for i, c in enumerate(sources):
        if type(c).__name__ not in _DC_ENTRY_TYPES:
            raise _OpError(
                'SELECTOR_TYPE',
                'sources[' + str(i) + '] is a ' + type(c).__name__ + ', not a PyClip / PySequence',
            )
    entries = _dc_dest_entries(dest)
    existing = _dc_rows(entries)
    if known is not None:
        for row, entry in zip(existing, entries):
            if row['uid'] not in known:
                row['state'] = _dm_state(entry)
    return {
        'destination': _dc_with_selector(dest),
        'existing': existing,
        'sources': [_dc_with_selector(c) for c in sources],
        'error': None,
        'errorCode': None,
    }


def _dc_describe_copies(out, dest, sources, copies, source_uids, existing_uids):
    for i, c in enumerate(copies):
        row = dict(_dc_with_selector(c), sourceUid=source_uids[i] if i < len(source_uids) else None)
        row['state'] = _dm_state(c)
        if row['uid'] is None or row['uid'] in source_uids or row['uid'] in existing_uids:
            out['notNew'].append(row)
        else:
            out['copies'].append(row)
    after = _dc_dest_entries(dest)
    after_uids = [_try(lambda o=o: _v(o.uid)) for o in after]
    out['destinationEntriesAfter'] = _dc_names(after)
    out['sourcesRemain'] = all(
        u is not None and _dc_find_by_uid(u) is not None for u in source_uids
    )
    if not out['sourcesRemain']:
        out['warnings'].append(
            'a source is not found by its uid after the copy: the call moved it or deleted it'
        )
    listed = [c['uid'] in after_uids for c in out['copies']]
    for row, flag in zip(out['copies'], listed):
        row['inDestination'] = flag
    if len(copies) != len(sources):
        out['error'] = (
            'copy returned ' + str(len(copies)) + ' clip(s) for ' + str(len(sources))
            + ' source(s)'
        )
        out['errorCode'] = 'SILENT_NOOP'
    elif out['notNew']:
        out['error'] = 'copy returned a clip that is not new (a source, or one that was there)'
        out['errorCode'] = 'SILENT_NOOP'
    elif type(dest).__name__ == 'PyReel' and not all(listed):
        out['error'] = (
            'copy returned without an error but the reel does not list every new clip '
            '(had ' + repr(out['destinationEntriesBefore']) + ', now '
            + repr(out['destinationEntriesAfter']) + ')'
        )
        out['errorCode'] = 'SILENT_NOOP'
    elif not all(listed):
        out['warnings'].append(
            'the destination (' + type(dest).__name__ + ') does not list every new clip'
        )


def _dc_copy(dest, sources, expected):
    if any(type(c).__name__ == 'PyBatch' for c in sources):
        return _dcb_copy(dest, sources, expected)
    snapshot = _dc_copy_snapshot(dest, sources)
    existing = _dc_dest_entries(dest)
    existing_uids = [_try(lambda o=o: _v(o.uid)) for o in existing]
    out = {
        'destination': snapshot['destination'],
        'requested': len(sources),
        'returned': 0,
        'copies': [],
        'notNew': [],
        'sources': snapshot['sources'],
        'sourcesRemain': None,
        'destinationEntriesBefore': _dc_names(existing),
        'destinationEntriesAfter': None,
        'display': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if expected is not None and sorted(existing_uids, key=str) != sorted(expected, key=str):
        out['error'] = 'nothing was copied: the destination changed between the read and the write'
        out['errorCode'] = 'CONFLICT'
        return out
    source_uids = [s['uid'] for s in snapshot['sources']]
    shown_before = _tl_shown()
    try:
        returned = flame.media_panel.copy(
            sources[0] if len(sources) == 1 else list(sources), dest, duplicate_action='add'
        )
    finally:
        out['display'] = _tl_copy_display(shown_before)
    copies = list(returned) if isinstance(returned, (list, tuple)) else []
    out['returned'] = len(copies)
    try:
        _dc_describe_copies(out, dest, sources, copies, source_uids, existing_uids)
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the copy was made but reading it back failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _dc_revert_library_open(destination, c, out):
    # a copy that went into a library / a folder is looked for in it; a closed library may list
    # nothing, which would read as "already deleted", so it is a conflict instead
    kind = destination.get('type') if isinstance(destination, dict) else None
    if kind not in ('PyLibrary', 'PyFolder'):
        return True
    container = _dm_container(destination)
    if container is None:
        problem = None
        if destination.get('type') == 'PyFolder':
            problem = (
                'the folder it went into could not be found (its library may be closed): open '
                'the library first'
            )
    else:
        problem = _dc_library_problem(container)
    if problem is None:
        return True
    out['conflicts'].append(repr(c.get('name')) + ' (uid ' + str(c.get('uid')) + '): ' + problem)
    return False


def _dc_revert_copies(created, plan_only=False):
    out = {
        'deleted': [],
        'skipped': [],
        'errors': [],
        'conflicts': [],
        'error': None,
        'errorCode': None,
    }
    found = []
    destination = created.get('destination')
    has_batch = any(c.get('type') == 'PyBatch' for c in created.get('copies') or [])
    current_before = _dc_current() if has_batch else None
    for c in created.get('copies') or []:
        uid = c.get('uid')
        chain = list(c.get('uidChain') or ([uid] if uid else []))
        is_batch = c.get('type') == 'PyBatch'
        if is_batch and not _dc_revert_library_open(destination, c, out):
            continue
        obj, found_uid = _dm_find_chain(chain, destination if is_batch else None)
        if obj is None:
            out['skipped'].append(
                {
                    'uid': uid,
                    'uidChain': chain,
                    'name': c.get('name'),
                    'reason': 'not found (already deleted)',
                }
            )
            continue
        uid = found_uid
        if type(obj).__name__ not in (('PyBatch',) if is_batch else _DC_ENTRY_TYPES):
            out['errors'].append(
                {'uid': uid, 'name': c.get('name'), 'error': 'is a ' + type(obj).__name__}
            )
            continue
        if is_batch:
            problems = _dcb_state_problems(c.get('state'), _dcb_state(obj))
        else:
            problems = _dm_state_problems(c.get('state'), _dm_state(obj), False, 'clip')
        for problem in problems:
            out['conflicts'].append(repr(c.get('name')) + ' (uid ' + str(uid) + '): ' + problem)
        found.append((c, obj, chain, found_uid))
    if out['conflicts']:
        out['error'] = 'nothing was deleted: ' + '; '.join(out['conflicts'])
        out['errorCode'] = 'CONFLICT'
        return out
    if plan_only:
        # nothing is deleted: what a revert would delete, so the server can save the setup of a
        # Batch group of the Desktop first
        out['plan'] = [
            {
                'uid': c.get('uid'),
                'foundUid': uid,
                'name': c.get('name'),
                'onDesktop': c.get('type') == 'PyBatch' and _dcb_on_desktop(uid),
            }
            for c, obj, chain, uid in found
        ]
        return out
    for c, obj, chain, uid in found:
        is_batch = c.get('type') == 'PyBatch'
        returned = flame.delete(obj, confirm=False)
        still = _dm_find(uid, destination) if is_batch else _dc_find_by_uid(uid)
        if returned is True and still is None:
            out['deleted'].append(
                {'uid': c.get('uid'), 'foundUid': uid, 'uidChain': chain, 'name': c.get('name')}
            )
        else:
            out['errors'].append(
                {
                    'uid': uid,
                    'name': c.get('name'),
                    'error': 'flame.delete returned ' + repr(_ja(returned)) + ', the clip is '
                    + ('still there' if still is not None else 'gone'),
                }
            )
    if has_batch:
        out['previousCurrent'] = current_before
        out['current'] = _dc_current()
        out['currentChanged'] = (out['current'] or {}).get('uid') != (current_before or {}).get(
            'uid'
        )
    if out['errors']:
        out['error'] = (
            str(len(out['errors'])) + ' of ' + str(len(created.get('copies') or []))
            + ' copies could not be deleted'
        )
        out['errorCode'] = 'SILENT_NOOP'
    return out
"""
