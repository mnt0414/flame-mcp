"""Prelude source for `desktop.delete` (`ops/desktop_delete.py`, `desktop_delete_run.py`).

Like `desktop_media_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError` and
the `_dc_*` / `_dm_*` / `_tl_*` helpers already defined.

What it encodes (measured on Flame 2025.2.4):
  - `flame.delete(obj, confirm=False)` deletes a clip, a sequence, a reel and a reel group and
    answers True; afterwards the uid finds nothing. So a delete is judged by looking the uid up
    again, not by the answer.
  - what is deleted is copied first into the stash reel of the checkpoint library
    (`media_panel.copy`: the same segments, effect settings and pixels), and the copy's
    fingerprint must be the original's: `_tl_stash_copy`, which unlike `timeline.checkpoint` does
    not mind a library entry. A revert copies the stashed copy back into the original parent: a new
    uid, the end of the reel.
  - what the Timeline shows may change when something is moved out of a reel or the shown
    sequence is deleted (observed; no rule is known for when), so what is shown is read before and
    after, the sequence shown before being opened again when it changed (`_tl_display_fix`).
  - only a clip and a sequence are deleted: stashing and putting back a container (a reel, a reel
    group, a folder, a library, a Batch group) was not measured.
  - nothing inside a library is deleted (`_dd_check_target`, before anything is copied): library
    contents are never deleted by this server. `PURGE_HELPER` is the one exception, for the copies
    the journal recorded in the checkpoint library.

`PURGE_HELPER` is the Flame side of `journal.purge_checkpoints` (`timeline_purge.py`): a read of
what the stash library holds (so what the journal does not know is reported, never deleted) and the
delete of what the journal recorded, each checked in Flame to be inside the library with the uid and
the name the journal recorded, and of the date reels that this left empty.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

DELETE_HELPER = """
def _dd_inside_library(obj, library_name):
    for parent in _tl_chain(obj):
        if type(parent).__name__ != 'PyLibrary':
            continue
        if _try(lambda p=parent: _v(p.name)) == library_name:
            return True
    return False


def _dd_check_target(obj, label, library_name):
    kind = type(obj).__name__
    if kind not in _DC_ENTRY_TYPES:
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is a ' + kind + ', not a PyClip / PySequence: a container (a reel, a reel '
            'group, a folder, a library, a Batch group) is not deleted here, only a clip or a '
            'sequence is',
        )
    parent = _dm_parent(obj, label)
    if _dc_is_batch_reel(parent) or _tl_location(obj) == 'batch':
        raise _OpError(
            'INVALID_ARGS',
            label + ' is inside a reel of a Batch group: a Batch group is not deleted from here',
        )
    if _dd_inside_library(obj, library_name):
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is inside the checkpoint library ' + repr(library_name) + ': what is kept '
            'there is cleaned up with journal.purge_checkpoints, which only deletes what the '
            'journal records',
        )
    if _tl_location(obj) == 'library':
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is inside a library: library contents are never deleted by this server',
        )
    return parent


def _dd_snapshot(targets, library_name):
    rows = []
    seen = []
    for i, obj in enumerate(targets):
        label = 'targets[' + str(i) + ']'
        parent = _dd_check_target(obj, label, library_name)
        row = _dc_with_selector(obj)
        parent_row = _dc_with_selector(parent)
        if row['uid'] is None or row['selector'] is None or parent_row['selector'] is None:
            raise _OpError(
                'PRECONDITION_UNMET',
                label + ' or its parent has no uid / wiretap id, so it could not be found again '
                'or put back',
            )
        if row['uid'] in seen:
            raise _OpError('INVALID_ARGS', label + ' is listed twice')
        seen.append(row['uid'])
        fp = _tl_fingerprint(obj)
        row['parent'] = parent_row
        row['fingerprint'] = fp['fingerprint']
        row['segmentCount'] = fp['segmentCount']
        row['effectCount'] = fp['effectCount']
        row['displayed'] = _tl_is_displayed(obj)
        row['location'] = _tl_location(obj)
        rows.append(row)
    return {
        'sources': rows,
        'existing': [{'uid': r['uid']} for r in rows],
        'shown': _tl_shown(),
        'error': None,
        'errorCode': None,
    }


def _dd_stash(targets, library_name, reel_name):
    out = {
        'stashes': [],
        'libraryCreated': False,
        'reelCreated': False,
        'failed': None,
        'error': None,
        'errorCode': None,
    }
    for i, obj in enumerate(targets):
        try:
            res = _tl_stash_copy(obj, library_name, reel_name)
        except BaseException as exc:
            text = type(exc).__name__ + ': ' + str(exc)
            out['failed'] = {'index': i, 'stash': None, 'error': text}
            out['error'] = 'targets[' + str(i) + ']: the copy raised ' + text
            out['errorCode'] = 'FLAME_API_ERROR'
            return out
        out['libraryCreated'] = out['libraryCreated'] or res['libraryCreated']
        out['reelCreated'] = out['reelCreated'] or res['reelCreated']
        if res['error'] is not None:
            out['failed'] = {'index': i, 'stash': res['stash'], 'error': res['error']}
            out['error'] = 'targets[' + str(i) + ']: ' + res['error']
            out['errorCode'] = res['errorCode']
            return out
        out['stashes'].append(
            {
                'index': i,
                'source': res['source'],
                'stash': res['stash'],
                'fingerprint': res['fingerprint'],
                'segmentCount': res['segmentCount'],
                'effectCount': res['effectCount'],
                'displayed': res['displayed'],
                'display': res['display'],
            }
        )
    return out


def _dd_delete_conflicts(targets, uids, snapshot, expected):
    if _dm_changed(uids, expected):
        return ['a target is not the one the read saw (the uids found are ' + repr(uids) + ')']
    problems = []
    for obj, uid in zip(targets, uids):
        name = _try(lambda o=obj: _v(o.name))
        info = snapshot.get(uid)
        if info is None:
            problems.append(repr(name) + ' (uid ' + str(uid) + ') has no stashed copy on record')
        elif _tl_fingerprint(obj)['fingerprint'] != info['fingerprint']:
            problems.append(
                repr(name) + ' (uid ' + str(uid) + ') changed between the read and the write'
            )
    return problems


def _dd_delete_one(obj, uid, info, shown_before):
    parent = _try(lambda: obj.parent)
    origin = _dc_with_selector(obj)
    origin['parent'] = _dc_with_selector(parent) if parent is not None else None
    try:
        returned = flame.delete(obj, confirm=False)
        error = None
    except BaseException as exc:
        returned, error = None, type(exc).__name__ + ': ' + str(exc)
    still = _dc_find_by_uid(uid)
    row = {
        'from': {
            'parent': origin['parent'],
            'name': origin['name'],
            'type': origin['type'],
            'uid': uid,
        },
        'checkpoint': info.get('checkpoint'),
        'stash': info.get('stash'),
        'fingerprint': info.get('fingerprint'),
        'segmentCount': info.get('segmentCount'),
        'wasDisplayed': shown_before is not None and shown_before['uid'] == uid,
        'deleteReturned': _ja(returned),
        'deleteError': error,
    }
    return still is None, row, still


def _dd_delete(targets, snapshot, expected):
    out = {
        'requested': len(targets),
        'deleted': [],
        'notDeleted': [],
        'partial': False,
        'shownBefore': None,
        'shownAfterDelete': None,
        'shownAfter': None,
        'reshown': None,
        'reshownNote': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    uids = [_dm_uid(o) for o in targets]
    problems = _dd_delete_conflicts(targets, uids, snapshot, expected)
    if problems:
        out['error'] = 'nothing was deleted: ' + '; '.join(problems)
        out['errorCode'] = 'CONFLICT'
        return out
    shown_before = _tl_shown()
    out['shownBefore'] = shown_before
    for obj, uid in zip(targets, uids):
        gone, row, still = _dd_delete_one(obj, uid, snapshot[uid], shown_before)
        if gone:
            out['deleted'].append(row)
            if row['deleteReturned'] is not True:
                out['warnings'].append(
                    repr(row['from']['name']) + ' is gone but flame.delete answered '
                    + repr(row['deleteReturned']) + ' / ' + repr(row['deleteError'])
                )
        else:
            row['stillThere'] = True
            out['notDeleted'].append(row)
    fix = _tl_display_fix(shown_before, False, None)
    out['shownAfterDelete'] = fix['afterStep']
    out['reshown'] = fix['reshown']
    out['reshownNote'] = fix['note']
    out['shownAfter'] = fix['after']
    if not out['deleted']:
        out['error'] = (
            'flame.delete left every one of the ' + str(len(targets)) + ' target(s) where it was '
            + repr([r['deleteReturned'] for r in out['notDeleted']])
            + ' / ' + repr([r['deleteError'] for r in out['notDeleted']])
        )
        out['errorCode'] = 'SILENT_NOOP'
        return out
    out['partial'] = len(out['notDeleted']) > 0
    if out['partial']:
        out['warnings'].append(
            str(len(out['notDeleted'])) + ' of ' + str(len(targets)) + ' target(s) are still '
            'there (notDeleted)'
        )
    return out


def _dd_revert_problem(item, stash):
    if stash is None:
        return 'its stashed copy is gone (deleted, or purged by journal.purge_checkpoints)'
    wanted = (item.get('from') or {}).get('type')
    if type(stash).__name__ != wanted:
        return 'its stashed copy is a ' + type(stash).__name__ + ', not a ' + str(wanted)
    if _tl_fingerprint(stash)['fingerprint'] != item.get('fingerprint'):
        return 'its stashed copy has changed since it was made'
    return None


def _dd_revert_one(item):
    origin = item.get('from') or {}
    stash_row = item.get('stash') or {}
    stash = _dc_find_by_uid(stash_row.get('uid')) if stash_row.get('uid') else None
    problem = _dd_revert_problem(item, stash)
    parent = None
    if problem is None:
        parent = _dm_container(origin.get('parent'))
        if parent is None:
            problem = (
                'its original parent ' + repr((origin.get('parent') or {}).get('name'))
                + ' is gone'
            )
        elif type(parent).__name__ not in _DC_DEST_TYPES:
            problem = 'its original parent is a ' + type(parent).__name__ + ', not a reel / library'
    base = {'from': origin, 'checkpoint': item.get('checkpoint'), 'stash': stash_row}
    if problem is not None:
        return None, dict(base, reason=problem, errorCode='CONFLICT')
    try:
        restored, got, copy_problem = _tl_copy_into(stash, parent)
    except BaseException as exc:
        restored, copy_problem = None, type(exc).__name__ + ': ' + str(exc)
    if restored is None:
        reason = 'the stashed copy could not be copied back: ' + str(copy_problem)
        return None, dict(base, reason=reason, errorCode='FLAME_API_ERROR')
    row = dict(
        base,
        to=_dc_with_selector(restored),
        fingerprintMatches=_tl_fingerprint(restored)['fingerprint'] == item.get('fingerprint'),
        wasDisplayed=item.get('wasDisplayed'),
    )
    return restored, row


def _dd_revert(created):
    out = {
        'reverted': [],
        'notReverted': [],
        'partial': False,
        'shownBefore': None,
        'shownAfterRestore': None,
        'shownAfter': None,
        'reshown': None,
        'reshownNote': None,
        'error': None,
        'errorCode': None,
    }
    shown_before = _tl_shown()
    out['shownBefore'] = shown_before
    reopen = None
    for item in created.get('deletes') or []:
        restored, row = _dd_revert_one(item)
        if restored is None:
            out['notReverted'].append(row)
            continue
        out['reverted'].append(row)
        if item.get('wasDisplayed') and type(restored).__name__ == 'PySequence':
            reopen = restored
    if reopen is not None:
        out['shownAfterRestore'] = _tl_shown()
        out['reshown'], out['reshownNote'] = _tl_open(reopen)
        out['shownAfter'] = _tl_shown()
    else:
        fix = _tl_display_fix(shown_before, False, None)
        out['shownAfterRestore'] = fix['afterStep']
        out['reshown'] = fix['reshown']
        out['reshownNote'] = fix['note']
        out['shownAfter'] = fix['after']
    if out['notReverted']:
        out['partial'] = len(out['reverted']) > 0
        codes = sorted(set(r['errorCode'] for r in out['notReverted']))
        out['errorCode'] = codes[0] if len(codes) == 1 else 'FLAME_API_ERROR'
        out['error'] = (
            str(len(out['notReverted'])) + ' of ' + str(len(created.get('deletes') or []))
            + ' deleted thing(s) were not put back: '
            + '; '.join(
                repr((r['from'] or {}).get('name')) + ' ' + r['reason'] for r in out['notReverted']
            )
        )
    return out
"""

PURGE_HELPER = """
_TP_DEPTH = 4


def _tp_walk(container, reel_name, depth):
    # [(clip / sequence, name of the reel it is in)] under a library, folders and reels included
    # a container that cannot be listed stops the call: it must not read as "nothing is in it"
    found = []
    try:
        children = list(container.children)
    except BaseException as exc:
        raise _OpError(
            'FLAME_API_ERROR',
            'the checkpoint library could not be listed (' + type(exc).__name__ + ': ' + str(exc)
            + '): nothing was done',
        )
    for child in children:
        kind = type(child).__name__
        if kind in _DC_ENTRY_TYPES:
            found.append((child, reel_name))
        elif kind in ('PyReel', 'PyFolder') and depth > 0:
            name = _try(lambda c=child: _v(c.name)) if kind == 'PyReel' else reel_name
            found += _tp_walk(child, name, depth - 1)
    return found


def _tp_row(entry, reel):
    return {
        'type': type(entry).__name__,
        'name': _try(lambda: _v(entry.name)),
        'uid': _dm_uid(entry),
        'wiretapId': _try(lambda: entry.get_wiretap_node_id()),
        'reel': reel,
    }


def _tp_scan(library_name, candidates, known):
    out = {
        'libraryFound': False,
        'entries': [],
        'candidates': {},
        'unknown': [],
        'error': None,
        'errorCode': None,
    }
    lib = _tl_find_library(library_name)
    if lib is None:
        for cand in candidates:
            out['candidates'][cand['id']] = {'exists': False, 'nameMatches': False}
        return out
    out['libraryFound'] = True
    rows = [_tp_row(entry, reel) for entry, reel in _tp_walk(lib, None, _TP_DEPTH)]
    out['entries'] = rows
    by_uid = {}
    for row in rows:
        by_uid[row['uid']] = row
    for cand in candidates:
        row = by_uid.get(cand['uid'])
        out['candidates'][cand['id']] = {
            'exists': row is not None,
            'name': row['name'] if row is not None else None,
            'reel': row['reel'] if row is not None else None,
            'nameMatches': row is not None
            and (cand.get('name') is None or row['name'] == cand['name']),
        }
    out['unknown'] = [row for row in rows if row['uid'] not in known]
    return out


def _tp_delete_one(item, by_uid):
    found = by_uid.get(item['uid'])
    if found is None:
        return None, {'id': item['id'], 'reason': 'not in the checkpoint library (any more)'}
    entry, reel = found
    name = _try(lambda: _v(entry.name))
    if item.get('name') is not None and name != item['name']:
        return None, {
            'id': item['id'],
            'reason': 'its name is ' + repr(name) + ', the journal recorded ' + repr(item['name'])
            + ': it is not deleted',
        }
    try:
        returned = flame.delete(entry, confirm=False)
        error = None
    except BaseException as exc:
        returned, error = None, type(exc).__name__ + ': ' + str(exc)
    if _dc_find_by_uid(item['uid']) is not None:
        return None, {
            'id': item['id'],
            'reason': 'flame.delete answered ' + repr(_ja(returned)) + ' / ' + repr(error)
            + ' and it is still there',
        }
    return {'id': item['id'], 'uid': item['uid'], 'name': name, 'reel': reel}, None


def _tp_delete_empty_reels(lib, reels, prefix):
    deleted = []
    for reel in list(lib.reels):
        name = _try(lambda r=reel: _v(r.name))
        if not isinstance(name, str) or not name.startswith(prefix) or name not in reels:
            continue
        if len(list(reel.children)) > 0:
            continue
        uid = _dm_uid(reel)
        try:
            flame.delete(reel, confirm=False)
        except BaseException:
            pass
        if uid is not None and _dc_find_by_uid(uid) is None:
            deleted.append(name)
    return deleted


def _tp_purge(library_name, items, prefix):
    out = {'deleted': [], 'skipped': [], 'reelsDeleted': [], 'error': None, 'errorCode': None}
    lib = _tl_find_library(library_name)
    if lib is None:
        for item in items:
            out['skipped'].append(
                {'id': item['id'], 'reason': 'the checkpoint library is not there'}
            )
        return out
    by_uid = {}
    for entry, reel in _tp_walk(lib, None, _TP_DEPTH):
        by_uid[_dm_uid(entry)] = (entry, reel)
    touched = []
    for item in items:
        done, skipped = _tp_delete_one(item, by_uid)
        if done is not None:
            out['deleted'].append(done)
            touched.append(done['reel'])
        else:
            out['skipped'].append(skipped)
    out['reelsDeleted'] = _tp_delete_empty_reels(lib, touched, prefix)
    return out
"""
