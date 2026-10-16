"""Prelude source for the Timeline checkpoint ops (`ops/timeline_checkpoints.py`,
`timeline_restore.py`). The segment edits' own Flame code is in `timeline_trim_prelude`
(`segment.trim`, plan.run's up-front resolution) and `timeline_edit_prelude` (the rest).

Like `desktop_media_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code, so each op's `to_python` output is a few loop-free lines. It runs inside Flame (3.11)
with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`, `_segment_list`, `_wiretap_selector` and the
`_dc_*` / `_dm_*` helpers already defined.

What it encodes (measured on Flame 2025.2.4):
  - a sequence / clip is checkpointed as a copy in a stash reel of a library
    (`flame.media_panel.copy(seq, library reel)`: the copy has a new uid, the same segments, the
    same effect settings, the same pixels; a library copy cannot be edited, "Clip is locked.", but
    can be read and exported). The copy is found by comparing the reel's entries before and after.
  - the fingerprint is a sha256 over every version, every track, every segment: type, source_name,
    record_in / record_out / source_in / source_out (`.frame`, absolute), head, tail and the
    segment's effects (type and the value of every attribute `list(fx.attributes)` names). It does
    not see the name or the attributes of a segment, the name of the sequence, markers,
    transitions, audio, an effect's parameters that are not attributes, the playhead or the
    selection.
  - a restore moves the edited object into the stash reel (`media_panel.move`: a new uid and
    wiretap id, the end of the reel) and copies the stashed copy back into the object's parent
    (`media_panel.copy`: new uid again, the end of the parent). If the object was open in the
    Timeline, Flame then shows another sequence of the same reel; `PySequence.open()` (True) shows
    the restored one again. A library sequence's `open()` answers False.
  - `flame.timeline.clip` is what the Timeline shows (a PySequence); it is None when the shown
    sequence was deleted, and every read of it is guarded all the same.
  - which sequence the Timeline shows can change when a sequence is copied or moved.
    `_tl_stash_copy` reads what is shown before and after the copy and opens what was shown
    before again if it changed (`_tl_copy_display`); the desktop copy / duplicate do the same.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

TIMELINE_HELPER = """
_TL_MEDIA_TYPES = ('PyClip', 'PySequence')
_TL_PARENT_DEPTH = 8


def _tl_check_media(obj, label):
    kind = type(obj).__name__
    if kind not in _TL_MEDIA_TYPES:
        raise _OpError('SELECTOR_TYPE', label + ' is a ' + kind + ', not a PyClip / PySequence')


def _tl_chain(obj):
    chain = []
    cur = obj
    for _ in range(_TL_PARENT_DEPTH):
        try:
            cur = cur.parent
        except BaseException:
            break
        if cur is None:
            break
        chain.append(cur)
    return chain


def _tl_location(obj):
    # 'desktop' (a reel of a Desktop reel group), 'library', 'batch' (a reel of a Batch group) or
    # 'unknown' (the parents could not be followed to either)
    for parent in _tl_chain(obj):
        name = type(parent).__name__
        if name == 'PyLibrary':
            return 'library'
        if name == 'PyBatch' or (name == 'PyReel' and _dc_is_batch_reel(parent)):
            return 'batch'
        if name in ('PyReelGroup', 'PyDesktop'):
            return 'desktop'
    return 'unknown'


def _tl_chain_types(obj):
    return [type(p).__name__ for p in _tl_chain(obj)]


def _tl_refuse_location(obj, label):
    where = _tl_location(obj)
    if where == 'desktop':
        return None
    reasons = {
        'library': (
            'is inside a library: a library copy cannot be edited (Flame answers "Clip is '
            'locked.") and a checkpoint of it would be of nothing that changes'
        ),
        'batch': (
            'is inside a reel of a Batch group: a Batch group is not edited or checkpointed here'
        ),
        'unknown': (
            'is not found to be on the Desktop (its parents are ' + repr(_tl_chain_types(obj))
            + '): nothing is done to what cannot be put back'
        ),
    }
    return label + ' ' + reasons[where]


def _tl_get(fn, bad):
    try:
        return fn()
    except BaseException:
        bad[0] += 1
        return None


def _tl_fx_rows(seg, bad):
    rows = []
    for fx in _tl_get(lambda: list(seg.effects), bad) or []:
        ftype = _tl_get(lambda fx=fx: _j(fx.type), bad)
        names = _tl_get(lambda fx=fx: list(fx.attributes), bad) or []
        attrs = {}
        for key in names:
            attrs[key] = _tl_get(lambda fx=fx, key=key: _ja(getattr(fx, key)), bad)
        rows.append([ftype, attrs])
    return rows


def _tl_segment_row(seg, bad):
    return [
        _tl_get(lambda: _j(seg.type), bad),
        _tl_get(lambda: _v(seg.source_name), bad),
        _tl_get(lambda: seg.record_in.frame, bad),
        _tl_get(lambda: seg.record_out.frame, bad),
        _tl_get(lambda: seg.source_in.frame, bad),
        _tl_get(lambda: seg.source_out.frame, bad),
        _tl_get(lambda: _j(seg.head), bad),
        _tl_get(lambda: _j(seg.tail), bad),
        _tl_fx_rows(seg, bad),
    ]


def _tl_fingerprint(obj):
    import hashlib
    import json
    bad = [0]
    versions = []
    shape = []
    segment_count = 0
    effect_count = 0
    for ver in list(obj.versions):
        tracks = []
        counts = []
        for trk in list(ver.tracks):
            rows = [_tl_segment_row(s, bad) for s in list(trk.segments)]
            segment_count += len(rows)
            effect_count += sum(len(r[8]) for r in rows)
            counts.append(len(rows))
            tracks.append(rows)
        versions.append(tracks)
        shape.append(counts)
    blob = json.dumps(versions, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return {
        'fingerprint': hashlib.sha256(blob.encode('utf-8')).hexdigest(),
        'versions': shape,
        'segmentCount': segment_count,
        'effectCount': effect_count,
        'unreadable': bad[0],
    }


def _tl_ident(obj):
    entry = _dc_with_selector(obj)
    try:
        parent = obj.parent
    except BaseException:
        parent = None
    entry['parent'] = None
    if parent is not None:
        entry['parent'] = {
            'type': type(parent).__name__,
            'name': _try(lambda: _v(parent.name)),
            'uid': _try(lambda: _v(parent.uid)),
            'wiretapId': _try(lambda: parent.get_wiretap_node_id()),
        }
    return entry


def _tl_displayed_uid():
    try:
        shown = flame.timeline.clip
    except BaseException:
        return None
    if shown is None:
        return None
    return _try(lambda: _v(shown.uid))


def _tl_shown():
    # what the Timeline shows now: {type, uid, name}, None when nothing / unreadable
    try:
        shown = flame.timeline.clip
    except BaseException:
        return None
    if shown is None:
        return None
    uid = _try(lambda: _v(shown.uid))
    if uid is None:
        return None
    return {
        'type': type(shown).__name__,
        'uid': uid,
        'name': _try(lambda: _v(shown.name)),
    }


def _tl_open(obj):
    # (opened or not, a note): only a PySequence has open(), and a sequence inside a library does
    # not open (open() answers False)
    kind = type(obj).__name__
    if kind != 'PySequence':
        return False, 'a ' + kind + ' has no open(): the Timeline was not pointed at it'
    if _tl_location(obj) == 'library':
        return False, (
            'a sequence inside a library cannot be opened: the Timeline was not pointed at it'
        )
    try:
        return _ja(obj.open()), None
    except BaseException as exc:
        return False, type(exc).__name__ + ': ' + str(exc)


def _tl_display_fix(shown_before, was_displayed, new_obj):
    # The Timeline can show another sequence after a move: put back what was shown before the
    # step. No rule about when the display changes is relied on: what is shown is read again and
    # compared. `new_obj` is what the step made of the shown sequence (None when
    # it is gone). Answers {afterStep, reshown, note, opened, after}.
    out = {'afterStep': _tl_shown(), 'reshown': None, 'note': None, 'opened': False}
    if shown_before is None:
        out['after'] = out['afterStep']
        return out
    if was_displayed:
        target = new_obj
    else:
        now = out['afterStep']
        if now is not None and now['uid'] == shown_before['uid']:
            target = None
        else:
            target = _dc_find_by_uid(shown_before['uid'])
    if target is not None:
        out['opened'] = True
        out['reshown'], out['note'] = _tl_open(target)
    out['after'] = _tl_shown()
    return out


def _tl_copy_display(shown_before):
    # Re-read the display after a copy / duplicate of a sequence and reopen the original if it
    # changed. Called right after such a step: what is shown is read again and, when it is not
    # what was shown before, what was shown before is opened again. Does
    # nothing when nothing was shown before. Answers {shownBefore, shownAfterCopy, reshown, note,
    # shownAfter}; shownAfter is what is shown once it has been put back (read again).
    fix = _tl_display_fix(shown_before, False, None)
    note = fix['note']
    after_step = fix['afterStep']
    if (
        shown_before is not None
        and not fix['opened']
        and (after_step is None or after_step['uid'] != shown_before['uid'])
    ):
        note = (
            'what was shown before (uid ' + str(shown_before['uid']) + ') is not found any '
            'more: the Timeline was not pointed at it'
        )
    return {
        'shownBefore': shown_before,
        'shownAfterCopy': after_step,
        'reshown': fix['reshown'],
        'note': note,
        'shownAfter': fix['after'],
    }


def _tl_restore_display(out, shown_before, was_displayed, new_obj):
    fix = _tl_display_fix(shown_before, was_displayed, new_obj)
    out['shownAfterRestore'] = fix['afterStep']
    out['reshown'] = fix['reshown']
    if fix['opened']:
        if was_displayed:
            out['reopened'], out['reopenNote'] = fix['reshown'], fix['note']
        elif fix['note'] is not None:
            out['reshownNote'] = fix['note']
    out['shownAfter'] = fix['after']


def _tl_is_displayed(obj):
    shown = _tl_displayed_uid()
    uid = _dm_uid(obj)
    return shown is not None and uid is not None and shown == uid


def _tl_find_library(library_name):
    # a library that is closed (or whose state cannot be read) lists nothing, which would read as
    # "the library is empty / the copy is gone": it is never used. The call opens a named library
    # first (`library_open_prelude`); this stops what that did not reach
    workspace = flame.projects.current_project.current_workspace
    for lib in list(workspace.libraries):
        if _try(lambda lib=lib: _v(lib.name)) == library_name:
            problem = _dc_library_problem(lib)
            if problem is not None:
                raise _OpError('PRECONDITION_UNMET', problem + ': nothing was done')
            return lib
    return None


def _tl_stash_reel(library_name, reel_name):
    created_library = False
    created_reel = False
    lib = _tl_find_library(library_name)
    if lib is None:
        workspace = flame.projects.current_project.current_workspace
        lib = workspace.create_library(library_name)
        created_library = True
        if type(lib).__name__ != 'PyLibrary' or _tl_find_library(library_name) is None:
            raise _OpError(
                'FLAME_API_ERROR',
                'create_library(' + repr(library_name) + ') did not leave a library of that name',
            )
    reel = None
    for r in list(lib.reels):
        if _try(lambda r=r: _v(r.name)) == reel_name:
            reel = r
            break
    if reel is None:
        reel = lib.create_reel(reel_name)
        created_reel = True
        if type(reel).__name__ != 'PyReel':
            raise _OpError(
                'FLAME_API_ERROR',
                'create_reel(' + repr(reel_name) + ') in the library answered a '
                + type(reel).__name__,
            )
    return lib, reel, created_library, created_reel


def _tl_pick_new(new, got):
    if len(new) == 1:
        return new[0]
    answered = [_dm_uid(o) for o in got]
    hits = [o for o in new if _dm_uid(o) in answered]
    return hits[0] if len(hits) == 1 else None


def _tl_copy_into(obj, dest):
    # (the new entry of `dest` the copy made, the answer, an error text)
    before = [_dm_uid(o) for o in _dm_media_children(dest)]
    returned = flame.media_panel.copy(obj, dest)
    got = list(returned) if isinstance(returned, (list, tuple)) else []
    new = _dm_new_entries(before, _dm_media_children(dest))
    if not new:
        return None, got, (
            'media_panel.copy returned ' + str(len(got)) + ' object(s) and the reel did not grow'
        )
    made = _tl_pick_new(new, got)
    if made is None:
        return None, got, (
            str(len(new)) + ' new entries appeared in the reel and none of them can be told to '
            'be the copy (names ' + repr(_dc_names(new)) + ')'
        )
    return made, got, None


def _tl_stash_row(obj, library_name, reel_name):
    row = _dc_with_selector(obj)
    row['library'] = library_name
    row['reel'] = reel_name
    return row


def _tl_checkpoint(target, library_name, reel_name):
    _tl_check_media(target, 'target')
    refusal = _tl_refuse_location(target, 'the target')
    if refusal is not None:
        raise _OpError('PRECONDITION_UNMET', refusal)
    return _tl_stash_copy(target, library_name, reel_name)


def _tl_stash_copy(target, library_name, reel_name):
    # the stash itself, for any clip / sequence wherever it is (a library one too): `timeline.
    # checkpoint` refuses a library one as an op, the copy does not care
    shown_before = _tl_shown()
    fp = _tl_fingerprint(target)
    out = {
        'source': _tl_ident(target),
        'fingerprint': fp['fingerprint'],
        'segmentCount': fp['segmentCount'],
        'effectCount': fp['effectCount'],
        'versions': fp['versions'],
        'unreadable': fp['unreadable'],
        'displayed': _tl_is_displayed(target),
        'display': None,
        'stash': None,
        'stashFingerprint': None,
        'stashMatches': None,
        'libraryCreated': False,
        'reelCreated': False,
        'error': None,
        'errorCode': None,
    }
    lib, reel, out['libraryCreated'], out['reelCreated'] = _tl_stash_reel(library_name, reel_name)
    library_actual = _try(lambda: _v(lib.name))
    reel_actual = _try(lambda: _v(reel.name))
    try:
        made, got, problem = _tl_copy_into(target, reel)
    finally:
        out['display'] = _tl_copy_display(shown_before)
    out['answered'] = [type(o).__name__ for o in got]
    if made is None:
        out['error'] = 'the checkpoint copy was not made: ' + problem
        out['errorCode'] = 'SILENT_NOOP'
        return out
    out['stash'] = _tl_stash_row(made, library_actual, reel_actual)
    if type(made).__name__ != type(target).__name__:
        out['error'] = (
            'the copy is a ' + type(made).__name__ + ', not a ' + type(target).__name__
            + '; it was NOT deleted (uid ' + repr(out['stash']['uid']) + ')'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    stash_fp = _tl_fingerprint(made)
    out['stashFingerprint'] = stash_fp['fingerprint']
    out['stashMatches'] = stash_fp['fingerprint'] == fp['fingerprint']
    if not out['stashMatches']:
        out['error'] = (
            'the stashed copy does not have the fingerprint of the original ('
            + stash_fp['fingerprint'][:12] + ' against ' + fp['fingerprint'][:12]
            + '): it was NOT deleted (uid ' + repr(out['stash']['uid'])
            + ') and no checkpoint was recorded'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _tl_find_first(uids):
    for uid in uids:
        found = _dc_find_by_uid(uid)
        if found is not None:
            return found, uid
    return None, None


def _tl_restore_refusals(target, stash, stash_fp):
    # the reasons nothing may be touched, before any write
    if target is None:
        return 'SELECTOR_EMPTY', (
            'the sequence / clip the checkpoint is of is not found by its uid any more (a restore, '
            'a move or a delete gave it another one)'
        )
    kind = type(target).__name__
    if kind not in _TL_MEDIA_TYPES:
        return 'SELECTOR_TYPE', 'the target is a ' + kind + ', not a PyClip / PySequence'
    refusal = _tl_refuse_location(target, 'the target')
    if refusal is not None:
        return 'PRECONDITION_UNMET', refusal
    if stash is None:
        return 'PRECONDITION_UNMET', (
            'the stashed copy of the checkpoint is gone (deleted or moved): there is nothing to '
            'restore from'
        )
    if type(stash).__name__ != kind:
        return 'PRECONDITION_UNMET', (
            'the stashed copy is a ' + type(stash).__name__ + ', the target a ' + kind
        )
    if _tl_fingerprint(stash)['fingerprint'] != stash_fp:
        return 'PRECONDITION_UNMET', (
            'the stashed copy has changed since the checkpoint was taken (its fingerprint is not '
            'the recorded one): restoring from it would put back something else'
        )
    try:
        parent = target.parent
    except BaseException:
        parent = None
    if parent is None or type(parent).__name__ != 'PyReel':
        return 'PRECONDITION_UNMET', (
            'the target has no readable reel to put the restored copy into'
        )
    return None, None


def _tl_restore_read(uids, stash_uid, stash_fp):
    target, found_uid = _tl_find_first(uids)
    stash = _dc_find_by_uid(stash_uid) if stash_uid else None
    out = {
        'target': None,
        'foundUid': found_uid,
        'fingerprint': None,
        'segmentCount': None,
        'displayed': None,
        'location': None,
        'stash': None,
        'error': None,
        'errorCode': None,
    }
    code, message = _tl_restore_refusals(target, stash, stash_fp)
    if target is not None:
        fp = _tl_fingerprint(target)
        out['target'] = _tl_ident(target)
        out['fingerprint'] = fp['fingerprint']
        out['segmentCount'] = fp['segmentCount']
        out['displayed'] = _tl_is_displayed(target)
        out['location'] = _tl_location(target)
    if stash is not None:
        out['stash'] = _dc_with_selector(stash)
    if message is not None:
        out['error'] = message
        out['errorCode'] = code
    return out


def _tl_rollback(moved, parent):
    try:
        back, got, problem = _tl_move_into(moved, parent)
    except BaseException as exc:
        return {'done': False, 'error': type(exc).__name__ + ': ' + str(exc), 'object': None}
    if back is None:
        return {'done': False, 'error': problem, 'object': None}
    return {'done': True, 'error': None, 'object': _tl_ident(back)}


def _tl_move_into(obj, dest):
    # (the entry of `dest` the move made, the answer, an error text); the moved object has a new uid
    before = [_dm_uid(o) for o in _dm_media_children(dest)]
    returned = flame.media_panel.move(obj, dest)
    got = list(returned) if isinstance(returned, (list, tuple)) else []
    new = _dm_new_entries(before, _dm_media_children(dest))
    if not new:
        return None, got, (
            'media_panel.move returned ' + str(len(got)) + ' object(s) and the reel did not grow'
        )
    return _tl_pick_new(new, got) or new[0], got, None


def _tl_restore_write(uids, expected_fp, stash_uid, stash_fp, library_name, reel_name):
    out = {
        'restored': None,
        'preStash': None,
        'replacedUid': None,
        'fingerprintBefore': None,
        'fingerprintAfter': None,
        'fingerprintMatches': None,
        'preFingerprintMatches': None,
        'displayedBefore': None,
        'shownBefore': None,
        'shownAfterRestore': None,
        'shownAfter': None,
        'reshown': None,
        'reshownNote': None,
        'reopened': None,
        'reopenNote': None,
        'position': None,
        'moved': False,
        'rolledBack': None,
        'error': None,
        'errorCode': None,
    }
    target, found_uid = _tl_find_first(uids)
    stash = _dc_find_by_uid(stash_uid) if stash_uid else None
    code, message = _tl_restore_refusals(target, stash, stash_fp)
    if message is not None:
        out['error'] = 'nothing was restored: ' + message
        out['errorCode'] = code
        return out
    before = _tl_fingerprint(target)
    out['fingerprintBefore'] = before['fingerprint']
    if expected_fp is not None and before['fingerprint'] != expected_fp:
        out['error'] = (
            'nothing was restored: the target changed between the read and the write (its '
            'fingerprint is ' + before['fingerprint'][:12] + ', it was ' + str(expected_fp)[:12]
            + ')'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    parent = target.parent
    out['replacedUid'] = _dm_uid(target)
    out['displayedBefore'] = _tl_is_displayed(target)
    out['shownBefore'] = _tl_shown()
    lib, reel, out['libraryCreated'], out['reelCreated'] = _tl_stash_reel(library_name, reel_name)
    library_actual = _try(lambda: _v(lib.name))
    reel_actual = _try(lambda: _v(reel.name))
    try:
        moved, got, problem = _tl_move_into(target, reel)
    except BaseException as exc:
        moved, problem = None, type(exc).__name__ + ': ' + str(exc)
    if moved is None:
        still = _dc_find_by_uid(out['replacedUid'])
        out['error'] = (
            'nothing was restored: the edited ' + type(target).__name__ + ' could not be moved '
            'into the stash reel (' + str(problem) + '); it '
            + ('is still where it was' if still is not None else 'is NOT found by its uid now')
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    out['moved'] = True
    out['preStash'] = _tl_stash_row(moved, library_actual, reel_actual)
    pre_fp = _tl_fingerprint(moved)
    out['preFingerprintMatches'] = pre_fp['fingerprint'] == before['fingerprint']
    try:
        restored, got, problem = _tl_copy_into(stash, parent)
    except BaseException as exc:
        restored, problem = None, type(exc).__name__ + ': ' + str(exc)
    if restored is None:
        out['rolledBack'] = _tl_rollback(moved, parent)
        back = out['rolledBack']
        if back['done']:
            fate = (
                'moved back into its reel (it has a NEW uid, ' + repr(back['object']['uid']) + ')'
            )
        else:
            fate = (
                'NOT moved back (' + str(back['error']) + '): it is in the stash reel, uid '
                + repr(out['preStash']['uid'])
            )
        out['error'] = (
            'the stashed copy could not be put back (' + str(problem) + '); the edited '
            + type(target).__name__ + ' was ' + fate
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        if back['done']:
            rolled = _dc_find_by_uid(back['object']['uid'])
            _tl_restore_display(out, out['shownBefore'], out['displayedBefore'], rolled)
        return out
    out['restored'] = _tl_ident(restored)
    _tl_restore_display(out, out['shownBefore'], out['displayedBefore'], restored)
    after = _tl_fingerprint(restored)
    out['fingerprintAfter'] = after['fingerprint']
    out['fingerprintMatches'] = after['fingerprint'] == stash_fp
    children = _dm_media_children(parent)
    uids_now = [_dm_uid(o) for o in children]
    restored_uid = out['restored']['uid']
    out['position'] = {
        'index': uids_now.index(restored_uid) if restored_uid in uids_now else None,
        'of': len(children),
    }
    return out
"""
