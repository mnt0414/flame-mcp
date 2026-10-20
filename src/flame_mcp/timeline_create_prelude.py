"""Prelude source for `timeline.create_sequence` (`ops/timeline_create.py`, server side
`timeline_create_run`).

Like `desktop_media_prelude` (it uses its `_dm_*` helpers, `desktop_prelude`'s `_dc_*` and the
`_tl_*` helpers of `timeline_prelude`): real Python source that `execution.build_exec_code`
places ahead of an op's own code. It runs inside Flame (3.11).

What it encodes (measured on Flame 2025.2.4):
  - `reel.create_sequence(name=, video_tracks=, start_at=, frame_rate=, width=, height=,
    bit_depth=)` answers the new PySequence (name and uid readable) and the reel lists it.
    `frame_rate` is one of the ten strings of the error text ('23.976 fps' ... '60 fps'), anything
    else is a RuntimeError; `start_at` is `flame.PyTime('01:00:00:00', frame_rate)`; width /
    height / frame rate / start read back as given. `bit_depth` (left out when the caller gave
    none, so the project's default applies) takes only the integers 8, 10, 12, 16 and 32 and
    reads back as given (2025.2.4 and 2026.2.3). The same name can be used twice. The
    new sequence is one Gap one frame long, and the Timeline then shows it.
  - the sequence is found by the reel's entries before and after (never by the answer alone), and
    judged at revert by its name, duration and fingerprint (`_dm_state`).
  - `flame.delete(seq, confirm=False)` removes it; if it was the sequence the Timeline shows, the
    Timeline shows nothing afterwards, so the sequence shown before the create is opened again.
  - a Timeline restore of the sequence gives it a new uid: the revert is handed the chain of uids
    (newest first) and works on the first one that resolves.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

CREATE_HELPER = """
def _ts_check_parent(parent):
    kind = type(parent).__name__
    if kind != 'PyReel':
        raise _OpError('SELECTOR_TYPE', 'parent is a ' + kind + ', not a reel')
    if _dc_is_batch_reel(parent):
        raise _OpError(
            'INVALID_ARGS',
            'parent is a reel of a Batch group (type ' + repr(_ja(parent.type)) + '): a sequence '
            'is made in a reel of the Desktop',
        )
    where = _tl_location(parent)
    if where == 'library':
        raise _OpError(
            'PRECONDITION_UNMET',
            'parent is inside a library: the contents of a library are not changed by this server',
        )
    if where != 'desktop':
        raise _OpError(
            'PRECONDITION_UNMET',
            'parent is not found to be a reel of the Desktop (its parents are '
            + repr(_tl_chain_types(parent)) + ')',
        )


def _ts_snapshot(parent):
    _ts_check_parent(parent)
    return {
        'parent': _dc_with_selector(parent),
        'existing': _dc_rows(_dm_media_children(parent)),
        'shown': _tl_shown(),
        'error': None,
        'errorCode': None,
    }


def _ts_facts(seq, want_start):
    tracks = _try(lambda: len(list(list(seq.versions)[0].tracks)))
    return {
        'frameRate': _try(lambda: _j(seq.frame_rate)),
        'width': _try(lambda: _j(seq.width)),
        'height': _try(lambda: _j(seq.height)),
        'bitDepth': _try(lambda: _ja(seq.bit_depth)),
        'startFrame': _try(lambda: _ja(seq.start_frame)),
        'startTime': _try(lambda: _j(_v(seq.start_time))),
        'duration': _try(lambda: _j(seq.duration)),
        'videoTracks': tracks,
        'wantedStartFrame': want_start,
    }


def _ts_mismatches(facts, rate, width, height, tracks, bit_depth):
    out = []
    for key, got, want in (
        ('frame_rate', facts['frameRate'], rate),
        ('width', facts['width'], width),
        ('height', facts['height'], height),
        ('video_tracks', facts['videoTracks'], tracks),
        ('bit_depth', facts['bitDepth'], bit_depth),
    ):
        if key == 'bit_depth' and want is None:
            continue
        if got != want:
            out.append(key + ' asked for ' + repr(want) + ', read back ' + repr(got))
    start = facts['startTime']
    got_start = start.get('frame') if isinstance(start, dict) else None
    if facts['wantedStartFrame'] is not None and got_start != facts['wantedStartFrame']:
        out.append(
            'start_tc asked for frame ' + repr(facts['wantedStartFrame']) + ', read back '
            + repr(got_start)
        )
    return out


def _ts_create(parent, name, tracks, rate, width, height, start_tc, expected, bit_depth):
    snapshot = _ts_snapshot(parent)
    before = _dm_media_children(parent)
    before_uids = [_dm_uid(o) for o in before]
    out = {
        'requested': {'name': name, 'videoTracks': tracks, 'frameRate': rate, 'width': width,
                      'height': height, 'startTc': start_tc, 'bitDepth': bit_depth},
        'parent': snapshot['parent'],
        'shownBefore': snapshot['shown'],
        'shownAfter': None,
        'returned': None,
        'created': None,
        'sequence': None,
        'state': None,
        'entriesBefore': _dc_names(before),
        'entriesAfter': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if _dm_changed(before_uids, expected):
        out['error'] = 'nothing was created: the reel changed between the read and the write'
        out['errorCode'] = 'CONFLICT'
        return out
    kwargs = {'name': name, 'video_tracks': tracks, 'frame_rate': rate, 'width': width,
              'height': height}
    if bit_depth is not None:
        kwargs['bit_depth'] = bit_depth
    want_start = 0
    if start_tc is not None and start_tc != '00:00:00:00':
        stamp = flame.PyTime(start_tc, rate)
        want_start = stamp.frame
        kwargs['start_at'] = stamp
    returned = parent.create_sequence(**kwargs)
    out['returned'] = {
        'type': type(returned).__name__,
        'name': _try(lambda: _v(returned.name)),
        'uid': _try(lambda: _v(returned.uid)),
    }
    try:
        out['shownAfter'] = _tl_shown()
        after = _dm_media_children(parent)
        out['entriesAfter'] = _dc_names(after)
        new = _dm_new_entries(before_uids, after)
        if not new:
            out['error'] = (
                'create_sequence returned without an error but the reel did not grow (had '
                + repr(out['entriesBefore']) + ', now ' + repr(out['entriesAfter']) + ')'
            )
            out['errorCode'] = 'SILENT_NOOP'
            return out
        made = _tl_pick_new(new, [returned])
        if made is None:
            out['candidates'] = [_dc_with_selector(o) for o in new]
            out['error'] = (
                str(len(new)) + ' new entries appeared in the reel and none of them can be told '
                'to be the sequence (names ' + repr(_dc_names(new)) + '): nothing is recorded as '
                'made, so nothing can be reverted'
            )
            out['errorCode'] = 'FLAME_API_ERROR'
            return out
        out['created'] = _dc_with_selector(made)
        if type(made).__name__ != 'PySequence':
            out['warnings'].append('the new entry is a ' + type(made).__name__)
        out['sequence'] = _ts_facts(made, want_start)
        out['warnings'] += [
            'read-back differs: ' + m
            for m in _ts_mismatches(out['sequence'], rate, width, height, tracks, bit_depth)
        ]
        out['state'] = _dm_state(made)
        if len(new) > 1:
            out['warnings'].append(
                str(len(new)) + ' new entries appeared; the one Flame answered is taken'
            )
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the sequence was made but reading it back failed: ' + type(exc).__name__ + ': '
                + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _ts_revert(created):
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
        'shownBefore': None,
        'shownAfterDelete': None,
        'reshown': None,
        'reshownNote': None,
        'shownAfter': None,
        'error': None,
        'errorCode': None,
    }
    obj = None
    for candidate in out['uidChain']:
        obj = _dm_find(candidate, created.get('parent'))
        if obj is not None:
            out['foundUid'] = candidate
            break
    if obj is None:
        out['alreadyGone'] = True
        return out
    uid = out['foundUid']
    if type(obj).__name__ != created.get('type'):
        out['error'] = (
            'the object with uid ' + repr(uid) + ' is a ' + type(obj).__name__ + ', not a '
            + str(created.get('type')) + ': nothing was deleted'
        )
        out['errorCode'] = 'SELECTOR_TYPE'
        return out
    state = _dm_state(obj)
    out['currentState'] = state
    problems = _dm_state_problems(created.get('state'), state, False, 'sequence')
    if problems:
        out['conflicts'] = problems
        out['error'] = (
            'nothing was deleted: the sequence has changed since it was made ('
            + '; '.join(problems) + ')'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    shown = _tl_shown()
    out['shownBefore'] = shown
    was_shown = shown is not None and shown['uid'] == uid
    gone, returned, still = _dm_delete(obj, uid, created.get('parent'))
    out['deleteReturned'] = _ja(returned)
    out['deleted'] = gone
    out['shownAfterDelete'] = _tl_shown()
    previous = created.get('shownBefore')
    if gone and was_shown and isinstance(previous, dict) and previous.get('uid'):
        target = _dc_find_by_uid(previous['uid'])
        if target is not None:
            out['reshown'], out['reshownNote'] = _tl_open(target)
    out['shownAfter'] = _tl_shown()
    if not gone:
        out['error'] = (
            'flame.delete returned ' + repr(_ja(returned)) + ' and the sequence is '
            + ('still there' if still is not None else 'gone')
        )
        out['errorCode'] = 'SILENT_NOOP'
    return out
"""
