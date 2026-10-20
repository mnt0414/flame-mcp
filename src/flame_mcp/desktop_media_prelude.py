"""Prelude source for `mediahub.import`, `desktop.duplicate` and `desktop.move`
(`ops/mediahub.py`, `ops/desktop_media.py`).

Like `desktop_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code, so each op's `to_python` output is a few loop-free lines. It runs inside Flame (3.11) with
`flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError` and the `_dc_*` helpers of `desktop_prelude` already
defined.

What it encodes (measured on Flame 2025.2.4):
  - `flame.import_clips(path or [paths], destination)` answers the new clips. A path that does not
    exist and a 0-byte file make no clip and raise nothing: the answer is `[]`. A directory brings
    the media in it. So an import is judged by what the destination lists afterwards, never by the
    answer alone.
  - `flame.media_panel.move(x or [x], destination)` answers the moved objects in the order given. A
    moved object always has a new uid and a new wiretap id (an id it had before finds nothing any
    more) and goes to the end of the destination. So a move is judged by the destination's entries
    that were not there before, matched with the answer, and the old uid must be gone.
  - `flame.duplicate(clip)` answers a PyClip (name, uid readable) in the same reel;
    `flame.duplicate(reel)` a PyReel `<name> copy` at the end of its parent, contents copied;
    `flame.duplicate(sequence)` answers a wrapper typed PyClip whose name and uid may not read. So a
    duplicate is judged by the parent's children before and after, never by the answer.
  - `flame.delete(obj, confirm=False)` works on a clip, a sequence and a reel; what is gone is
    checked by uid afterwards.
  - a clip is moved from a library reel to a Desktop reel the same way (a new uid), but this
    server does not do it: a move whose source is inside a library is refused
    (`_dm_move_snapshot`); only the revert of a move into a library takes a thing out of one.
  - what a copy / import / duplicate made is judged at revert by its name, duration and
    fingerprint (`_dm_state`, `_dm_state_problems`): a record without a fingerprint is a conflict.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

MEDIA_HELPER = """
_DM_REEL_LIKE = ('PyReel', 'PyReelGroup', 'PyLibrary', 'PyFolder')


def _dm_uid(obj):
    return _try(lambda: _v(obj.uid))


def _dm_media_children(container):
    return [c for c in list(container.children) if type(c).__name__ in _DC_ENTRY_TYPES]


def _dm_pool(container):
    pool = []
    for attr in ('children', 'reels', 'batch_groups'):
        try:
            pool += list(getattr(container, attr) or [])
        except BaseException:
            pass
    return pool


def _dm_parent(obj, label):
    try:
        parent = obj.parent
    except BaseException:
        parent = None
    if parent is None or type(parent).__name__ not in _DM_REEL_LIKE:
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' has no readable parent (a reel, reel group, folder or library), so what '
            'this call makes could not be found again or put back',
        )
    return parent


def _dm_check_dest(dest):
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


def _dm_new_entries(before_uids, after):
    return [o for o in after if _dm_uid(o) is not None and _dm_uid(o) not in before_uids]


def _dm_unlisted(got, before_uids):
    # what Flame answered that is a new clip / sequence found by its uid, for a destination that
    # does not list it (a library or a folder: the listing of those was not measured)
    found = []
    for o in got:
        uid = _dm_uid(o)
        if (
            type(o).__name__ in _DC_ENTRY_TYPES
            and uid is not None
            and uid not in before_uids
            and _dc_find_by_uid(uid) is not None
        ):
            found.append(o)
    return found


def _dm_unlisted_warning(dest):
    return (
        'the destination (' + type(dest).__name__ + ') does not list the new entries; they are '
        'taken from what Flame answered and found by uid'
    )


def _dm_changed(uids, expected):
    return expected is not None and sorted(uids, key=str) != sorted(expected, key=str)


def _dm_container(entry):
    if not isinstance(entry, dict):
        return None
    found = _dc_find_parent(entry)
    if found is None and entry.get('wiretapId'):
        try:
            found = flame.find_by_wiretap_node_id(entry['wiretapId'])
        except BaseException:
            found = None
    if found is not None and entry.get('uid'):
        have = _dm_uid(found)
        if have is not None and have != entry['uid']:
            return None
    return found


def _dm_find(uid, parent_entry):
    if not uid:
        return None
    parent = _dm_container(parent_entry)
    if parent is not None:
        hit = _dc_scan(_dm_pool(parent), uid)
        if hit is not None:
            return hit
    return _dc_find_by_uid(uid)


def _dm_find_chain(chain, parent_entry):
    # a Timeline restore of a made clip / sequence gave it another uid: look it up by the newest
    # uid of its chain (newest first) that resolves; (the object, the uid that found it)
    for candidate in chain:
        obj = _dm_find(candidate, parent_entry)
        if obj is not None:
            return obj, candidate
    return None, None


def _dm_alive(items):
    found = {}
    for item in items:
        found[item.get('uid')] = _dm_find(item.get('uid'), item.get('parent')) is not None
    return {'alive': found, 'error': None, 'errorCode': None}


def _dm_probe(container, known, flags):
    if flags.get('reelsOnly'):
        try:
            entries = list(container.reels)
        except BaseException:
            entries = [c for c in list(container.children) if type(c).__name__ == 'PyReel']
    else:
        entries = _dm_media_children(container)
    rows = []
    for o in _dm_new_entries(known, entries):
        row = _dc_with_selector(o)
        if flags.get('withState'):
            row['state'] = _dm_state(o)
        rows.append(row)
    found = {}
    for uid in flags.get('uids') or []:
        found[uid] = _dc_find_by_uid(uid) is not None
    return {
        'container': _dc_with_selector(container),
        'new': rows,
        'count': len(entries),
        'found': found,
        'error': None,
        'errorCode': None,
    }


def _dm_delete(obj, uid, parent_entry):
    returned = flame.delete(obj, confirm=False)
    still = _dm_find(uid, parent_entry)
    return returned is True and still is None, returned, still


def _dm_dest_snapshot(dest):
    _dm_check_dest(dest)
    return {
        'destination': _dc_with_selector(dest),
        'existing': _dc_rows(_dm_media_children(dest)),
        'error': None,
        'errorCode': None,
    }


def _dm_import(dest, paths, expected):
    snapshot = _dm_dest_snapshot(dest)
    before = _dm_media_children(dest)
    before_uids = [_dm_uid(o) for o in before]
    out = {
        'destination': snapshot['destination'],
        'requested': len(paths),
        'returned': 0,
        'imported': [],
        'destinationEntriesBefore': _dc_names(before),
        'destinationEntriesAfter': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if _dm_changed(before_uids, expected):
        out['error'] = (
            'nothing was imported: the destination changed between the read and the write'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    returned = flame.import_clips(paths[0] if len(paths) == 1 else list(paths), dest)
    got = list(returned) if isinstance(returned, (list, tuple)) else []
    out['returned'] = len(got)
    try:
        after = _dm_media_children(dest)
        out['destinationEntriesAfter'] = _dc_names(after)
        new = _dm_new_entries(before_uids, after)
        if not new and type(dest).__name__ != 'PyReel':
            new = _dm_unlisted(got, before_uids)
            if new:
                out['warnings'].append(_dm_unlisted_warning(dest))
        out['imported'] = []
        for o in new:
            row = _dc_with_selector(o)
            row['state'] = _dm_state(o)
            out['imported'].append(row)
        if not out['imported']:
            out['error'] = (
                'import_clips returned ' + str(out['returned']) + ' clip(s) for '
                + str(len(paths)) + ' path(s) and the destination did not grow (had '
                + repr(out['destinationEntriesBefore']) + ', now '
                + repr(out['destinationEntriesAfter']) + '): Flame ignores a path that does '
                'not exist and a file of 0 bytes without an error'
            )
            out['errorCode'] = 'SILENT_NOOP'
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the import ran but reading the destination back failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _dm_revert_imports(created):
    return _dc_revert_copies({'copies': created.get('imports')})


def _dm_siblings(parent, target):
    if type(target).__name__ == 'PyReel':
        try:
            return list(parent.reels)
        except BaseException:
            return [c for c in list(parent.children) if type(c).__name__ == 'PyReel']
    return _dm_media_children(parent)


def _dm_state(obj):
    name = _try(lambda: _v(obj.name))
    if type(obj).__name__ == 'PyReel':
        return {'name': name, 'entries': _dc_entry_count(obj)}
    return {
        'name': name,
        'duration': _try(lambda: obj.duration.frame),
        'fingerprint': _try(lambda: _tl_fingerprint(obj)['fingerprint']),
    }


def _dm_dup_snapshot(target):
    kind = type(target).__name__
    if kind not in ('PyClip', 'PySequence', 'PyReel'):
        raise _OpError(
            'PRECONDITION_UNMET',
            'target is a ' + kind + ': only a clip, a sequence and a reel are duplicated',
        )
    if kind == 'PyReel' and _dc_is_batch_reel(target):
        raise _OpError(
            'INVALID_ARGS',
            'target is a reel of a Batch group: a Batch group is not duplicated here',
        )
    parent = _dm_parent(target, 'target')
    if kind != 'PyReel' and _dc_is_batch_reel(parent):
        raise _OpError(
            'INVALID_ARGS',
            'target is inside a reel of a Batch group: a Batch group is not duplicated here',
        )
    source = _dc_with_selector(target)
    parent_row = _dc_with_selector(parent)
    if source['selector'] is None or parent_row['selector'] is None:
        raise _OpError(
            'PRECONDITION_UNMET',
            'the target or its parent has no wiretap id, so it could not be found again',
        )
    source['parent'] = parent_row
    return {
        'source': source,
        'parent': parent_row,
        'existing': _dc_rows(_dm_siblings(parent, target)),
        'error': None,
        'errorCode': None,
    }


def _dm_pick_duplicate(new, source):
    if len(new) == 1:
        return new[0]
    wanted = (source['name'], str(source['name']) + ' copy')
    hits = [o for o in new if _try(lambda o=o: _v(o.name)) in wanted]
    return hits[0] if len(hits) == 1 else None


def _dm_duplicate(target, expected):
    snapshot = _dm_dup_snapshot(target)
    parent = _dm_parent(target, 'target')
    before = _dm_siblings(parent, target)
    before_uids = [_dm_uid(o) for o in before]
    out = {
        'source': snapshot['source'],
        'parent': snapshot['parent'],
        'returned': None,
        'duplicate': None,
        'state': None,
        'siblingsBefore': _dc_names(before),
        'siblingsAfter': None,
        'display': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if _dm_changed(before_uids, expected):
        out['error'] = (
            'nothing was duplicated: the entries beside the target changed between the read and '
            'the write'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    shown_before = _tl_shown()
    try:
        returned = flame.duplicate(target)
    finally:
        out['display'] = _tl_copy_display(shown_before)
    out['returned'] = {
        'type': type(returned).__name__,
        'name': _try(lambda: _v(returned.name)),
        'uid': _try(lambda: _v(returned.uid)),
    }
    try:
        after = _dm_siblings(parent, target)
        out['siblingsAfter'] = _dc_names(after)
        new = _dm_new_entries(before_uids, after)
        if not new:
            out['error'] = (
                'flame.duplicate returned without an error but the entries beside the target did '
                'not grow (had ' + repr(out['siblingsBefore']) + ', now '
                + repr(out['siblingsAfter']) + ')'
            )
            out['errorCode'] = 'SILENT_NOOP'
            return out
        made = _dm_pick_duplicate(new, snapshot['source'])
        if made is None:
            out['error'] = (
                str(len(new)) + ' new entries appeared beside the target and none of them can be '
                'told to be the duplicate (names ' + repr(_dc_names(new)) + '): nothing is '
                'recorded as made, so nothing can be reverted'
            )
            out['errorCode'] = 'FLAME_API_ERROR'
            out['candidates'] = [_dc_with_selector(o) for o in new]
            return out
        out['duplicate'] = _dc_with_selector(made)
        out['state'] = _dm_state(made)
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the duplicate was made but reading it back failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _dm_state_problems(baseline, current, is_reel, what):
    # what a revert compares before it deletes a thing the server made: name and duration (a reel:
    # its number of entries) and, for a clip / sequence, its fingerprint. A record that has no
    # fingerprint to compare with cannot be told apart from an edited one: it is a problem as
    # well.
    baseline = baseline or {}
    problems = []
    if not is_reel and not baseline.get('fingerprint'):
        problems.append(
            'no fingerprint of it was recorded when it was made, so it cannot be told whether it '
            'was edited since'
        )
    for key in ('name', 'entries', 'duration', 'fingerprint'):
        if key in baseline and baseline[key] != current.get(key):
            problems.append(
                key + ' is ' + repr(current.get(key)) + ' now, it was ' + repr(baseline[key])
                + ' when the ' + what + ' was made'
            )
    return problems


def _dm_revert_duplicate(created):
    uid = created.get('uid')
    out = {
        'uid': uid,
        'foundUid': None,
        'uidChain': list(created.get('uidChain') or [uid]),
        'name': created.get('name'),
        'deleted': False,
        'alreadyGone': False,
        'conflicts': [],
        'currentState': None,
        'error': None,
        'errorCode': None,
    }
    obj, found = _dm_find_chain(out['uidChain'], created.get('parent'))
    out['foundUid'] = found
    if obj is None:
        out['alreadyGone'] = True
        return out
    uid = found
    if type(obj).__name__ != created.get('type'):
        out['error'] = (
            'the object with uid ' + repr(uid) + ' is a ' + type(obj).__name__ + ', not a '
            + str(created.get('type')) + ': nothing was deleted'
        )
        out['errorCode'] = 'SELECTOR_TYPE'
        return out
    state = _dm_state(obj)
    out['currentState'] = state
    problems = _dm_state_problems(
        created.get('state'), state, created.get('type') == 'PyReel', 'duplicate'
    )
    if problems:
        out['conflicts'] = problems
        out['error'] = (
            'nothing was deleted: the duplicate has changed since it was made ('
            + '; '.join(problems) + ')'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    gone, returned, still = _dm_delete(obj, uid, created.get('parent'))
    out['deleteReturned'] = _ja(returned)
    out['deleted'] = gone
    if not gone:
        out['error'] = (
            'flame.delete returned ' + repr(_ja(returned)) + ' and the duplicate is '
            + ('still there' if still is not None else 'gone')
        )
        out['errorCode'] = 'SILENT_NOOP'
    return out


def _dm_move_snapshot(dest, sources):
    _dm_check_dest(dest)
    dest_row = _dc_with_selector(dest)
    rows = []
    seen = []
    for i, c in enumerate(sources):
        label = 'sources[' + str(i) + ']'
        if type(c).__name__ not in _DC_ENTRY_TYPES:
            raise _OpError(
                'SELECTOR_TYPE', label + ' is a ' + type(c).__name__ + ', not a PyClip / PySequence'
            )
        row = _dc_with_selector(c)
        parent = _dm_parent(c, label)
        if _dc_is_batch_reel(parent):
            raise _OpError(
                'INVALID_ARGS',
                label + ' is inside a reel of a Batch group: a Batch group is not moved from here',
            )
        if _tl_location(c) == 'library':
            raise _OpError(
                'PRECONDITION_UNMET',
                label + ' is inside a library: library contents are not moved by this server '
                '(use desktop.copy to take something out of a library)',
            )
        parent_row = _dc_with_selector(parent)
        if row['uid'] is None or row['selector'] is None or parent_row['selector'] is None:
            raise _OpError(
                'PRECONDITION_UNMET',
                label + ' or its parent has no uid / wiretap id, so it could not be found '
                'again or put back',
            )
        if row['uid'] in seen:
            raise _OpError('INVALID_ARGS', label + ' is listed twice')
        if parent_row['uid'] == dest_row['uid'] or parent_row['selector'] == dest_row['selector']:
            raise _OpError(
                'PRECONDITION_UNMET',
                label + ' is already in the destination: such a move is refused',
            )
        seen.append(row['uid'])
        row['parent'] = parent_row
        rows.append(row)
    return {
        'destination': dest_row,
        'existing': _dc_rows(_dm_media_children(dest)),
        'sources': rows,
        'error': None,
        'errorCode': None,
    }


def _dm_pair_moves(rows, got, new):
    claimed = []
    moved = []
    not_moved = []
    by_uid = {}
    for o in new:
        by_uid[_dm_uid(o)] = o
    for i, row in enumerate(rows):
        made = None
        if i < len(got):
            cand = _try(lambda g=got[i]: _v(g.uid))
            if cand in by_uid and cand not in claimed:
                made = by_uid[cand]
        if made is None:
            for o in new:
                if (
                    _dm_uid(o) not in claimed
                    and type(o).__name__ == row['type']
                    and _try(lambda o=o: _v(o.name)) == row['name']
                ):
                    made = o
                    break
        origin = {
            'parent': row['parent'],
            'name': row['name'],
            'type': row['type'],
            'uid': row['uid'],
        }
        if made is None:
            not_moved.append({'from': origin})
        else:
            claimed.append(_dm_uid(made))
            moved.append({'from': origin, 'to': _dc_with_selector(made)})
    return moved, not_moved, [o for o in new if _dm_uid(o) not in claimed]


def _dm_move_objects(dest, rows, objs):
    before = _dm_media_children(dest)
    before_uids = [_dm_uid(o) for o in before]
    returned = flame.media_panel.move(objs[0] if len(objs) == 1 else list(objs), dest)
    got = list(returned) if isinstance(returned, (list, tuple)) else []
    after = _dm_media_children(dest)
    new = _dm_new_entries(before_uids, after)
    unlisted = False
    if not new and type(dest).__name__ != 'PyReel':
        new = _dm_unlisted(got, before_uids)
        unlisted = len(new) > 0
    moved, not_moved, extra = _dm_pair_moves(rows, got, new)
    return {
        'unlisted': unlisted,
        'returned': len(got),
        'moved': moved,
        'notMoved': not_moved,
        'extra': extra,
        'before': _dc_names(before),
        'after': _dc_names(after),
    }


def _dm_display_after(out, shown_before, moves):
    # The Timeline may show another sequence after a move (observed; no rule is known for when).
    # What was shown before is read again and put back: the moved sequence that was shown is
    # opened where it is now (a library one cannot be opened: a note), otherwise the sequence
    # shown before.
    out['shownBefore'] = shown_before
    new_obj = None
    was_displayed = False
    if shown_before is not None:
        for m in moves:
            if m['from']['uid'] == shown_before['uid']:
                was_displayed = True
                new_obj = _dc_find_by_uid(m['to']['uid'])
    fix = _tl_display_fix(shown_before, was_displayed, new_obj)
    out['shownAfterMove'] = fix['afterStep']
    out['reshown'] = fix['reshown']
    out['reshownNote'] = fix['note']
    out['shownAfter'] = fix['after']


def _dm_move(dest, sources, expected):
    snapshot = _dm_move_snapshot(dest, sources)
    before_uids = [r['uid'] for r in snapshot['existing']]
    out = {
        'destination': snapshot['destination'],
        'requested': len(sources),
        'returned': 0,
        'moved': [],
        'notMoved': [],
        'partial': False,
        'destinationEntriesBefore': _dc_names(_dm_media_children(dest)),
        'destinationEntriesAfter': None,
        'shownBefore': None,
        'shownAfterMove': None,
        'shownAfter': None,
        'reshown': None,
        'reshownNote': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if _dm_changed(before_uids, expected):
        out['error'] = 'nothing was moved: the destination changed between the read and the write'
        out['errorCode'] = 'CONFLICT'
        return out
    shown_before = _tl_shown()
    done = _dm_move_objects(dest, snapshot['sources'], sources)
    out['returned'] = done['returned']
    out['moved'] = done['moved']
    out['notMoved'] = done['notMoved']
    out['destinationEntriesAfter'] = done['after']
    _dm_display_after(out, shown_before, done['moved'])
    if not done['moved']:
        out['error'] = (
            'media_panel.move returned ' + str(done['returned']) + ' object(s) for '
            + str(len(sources)) + ' source(s) and no source reached the destination (had '
            + repr(done['before']) + ', now ' + repr(done['after']) + ')'
        )
        out['errorCode'] = 'SILENT_NOOP'
        return out
    if done['unlisted']:
        out['warnings'].append(_dm_unlisted_warning(dest))
    out['partial'] = len(done['notMoved']) > 0
    if out['partial']:
        out['warnings'].append(
            str(len(done['notMoved'])) + ' of ' + str(len(sources)) + ' source(s) did not reach '
            'the destination (notMoved)'
        )
    if done['extra']:
        out['warnings'].append(
            str(len(done['extra'])) + ' new entr(ies) of the destination could not be matched with '
            'a source: ' + repr(_dc_names(done['extra']))
        )
    gone = [_dc_find_by_uid(m['from']['uid']) is None for m in done['moved']]
    if not all(gone):
        out['warnings'].append(
            'a source is still found by its old uid after the move (Flame did not change it)'
        )
    return out


def _dm_move_conflicts(created):
    conflicts = []
    plan = []
    dest = _dm_container(created.get('destination'))
    if dest is None:
        conflicts.append('the destination the things were moved into is gone')
        return conflicts, plan
    pool = _dm_pool(dest)
    for m in created.get('moves') or []:
        to = m.get('to') or {}
        origin = m.get('from') or {}
        obj = _dc_scan(pool, to.get('uid'))
        parent = _dm_container(origin.get('parent'))
        label = repr(to.get('name')) + ' (uid ' + str(to.get('uid')) + ')'
        if obj is None:
            conflicts.append(label + ' is not in the destination any more')
        if parent is None:
            conflicts.append(
                'the original parent ' + repr((origin.get('parent') or {}).get('name'))
                + ' of ' + label + ' is gone'
            )
        if obj is not None and parent is not None:
            plan.append((obj, parent, origin, to))
    return conflicts, plan


def _dm_revert_moves(created):
    out = {
        'reverted': [],
        'conflicts': [],
        'errors': [],
        'shownBefore': None,
        'shownAfterMove': None,
        'shownAfter': None,
        'reshown': None,
        'reshownNote': None,
        'error': None,
        'errorCode': None,
    }
    conflicts, plan = _dm_move_conflicts(created)
    if conflicts:
        out['conflicts'] = conflicts
        out['error'] = (
            'nothing was moved back: ' + '; '.join(conflicts)
        )
        out['errorCode'] = 'CONFLICT'
        return out
    shown_before = _tl_shown()
    dest_entry = created.get('destination')
    groups = []
    for obj, parent, origin, to in plan:
        key = _try(lambda p=parent: _v(p.uid)) or origin['parent'].get('wiretapId')
        for g in groups:
            if g['key'] == key:
                g['items'].append((obj, to))
                break
        else:
            groups.append({'key': key, 'parent': parent, 'items': [(obj, to)]})
    for g in groups:
        rows = [
            {'parent': dest_entry, 'name': to['name'], 'type': to['type'], 'uid': to['uid']}
            for obj, to in g['items']
        ]
        try:
            done = _dm_move_objects(g['parent'], rows, [obj for obj, to in g['items']])
        except BaseException as exc:
            out['errors'].append(
                'moving back into ' + repr(_try(lambda p=g['parent']: _v(p.name))) + ' failed: '
                + type(exc).__name__ + ': ' + str(exc)
            )
            continue
        out['reverted'] += done['moved']
        for row in done['notMoved']:
            out['errors'].append(
                repr(row['from']['name']) + ' did not reach ' + repr(
                    _try(lambda p=g['parent']: _v(p.name))
                )
            )
    _dm_display_after(out, shown_before, out['reverted'])
    if out['errors']:
        out['error'] = (
            str(len(out['errors'])) + ' problem(s) while moving back: ' + '; '.join(out['errors'])
        )
        out['errorCode'] = 'SILENT_NOOP'
    return out
"""
