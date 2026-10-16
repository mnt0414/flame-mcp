"""Prelude source for `segment.copy_to_media_panel` and `segment.match`
(`ops/segment_extract.py`; server side `segment_extract_run`).

Like `desktop_media_prelude` (it uses its `_dm_*` helpers, `desktop_prelude`'s `_dc_*` and the
`_tl_*` fingerprint of `timeline_prelude`): real Python source that `execution.build_exec_code`
places ahead of an op's own code. It runs inside Flame (3.11).

What it encodes (measured on Flame 2025.2.4, twice):
  - both calls take a segment out of a sequence as a new clip in a reel / folder / library. A
    segment with a Timewarp (used range 52 frames, head 10, tail 10, record 112-163, in a
    72-frame media):
      copy_to_media_panel               duration 52, start_frame 11, source 10-61, record 112-163,
                                        head 10, tail 10, with the effect
      match()                           duration 52, start_frame 1, in_mark 10 / out_mark 62, the
                                        whole media (source 0-71, head 0, tail 0), no effect
      match(preserve_handle=True)       duration 52, start_frame 11, source 10-61, record 10-61,
                                        head 10, tail 10, no effect
      match(include_timeline_fx=True)   duration 72, source 10-81, head 10, tail 'infinite', with
                                        the effect (a different shape from the others)
    With no handle all of them make a handle-0 clip and raise nothing.
  - the answer is "a PyClip or a list of PyClips" (docstring), so what was made is found by
    comparing the destination's entries before and after, never by the answer. A library or a
    folder may not list the new entry: then the answered clips are looked up by uid (a warning).
  - a segment inside a library can be taken out (it only reads); whatever Flame refuses is an error.
  - what the call made is judged by its fingerprint (`timeline_prelude._tl_fingerprint`: segments
    and effects) and its name and duration: `journal.revert` deletes it only while all three are
    still what was read right after the call.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

EXTRACT_HELPER = """
_EX_MAX_SEGMENTS = 8


def _ex_state(obj):
    # what journal.revert compares before it deletes the clip: name, duration, content
    fp = _try(lambda: _tl_fingerprint(obj)['fingerprint'])
    return {
        'name': _try(lambda: _v(obj.name)),
        'duration': _try(lambda: obj.duration.frame),
        'fingerprint': fp,
    }


def _ex_clip_facts(clip):
    # the clip as it is now: duration, start_frame, in / out marks (when it has them), the
    # segments of its first version (track, type, source, record, head, tail, effect types) and
    # the effect types it holds
    out = {
        'duration': _try(lambda: _j(clip.duration)),
        'startFrame': _try(lambda: _ja(clip.start_frame)),
        'inMark': _try(lambda: _ja(clip.in_mark)),
        'outMark': _try(lambda: _ja(clip.out_mark)),
        'segmentCount': 0,
        'segments': [],
        'effects': [],
    }
    versions = _try(lambda: list(clip.versions)) or []
    for ver in versions[:1]:
        for ti, trk in enumerate(list(ver.tracks)):
            for seg in list(trk.segments):
                out['segmentCount'] += 1
                if len(out['segments']) < _EX_MAX_SEGMENTS:
                    row = _try(lambda seg=seg: _tl_seg_facts(seg)) or {}
                    row['track'] = ti
                    row['effects'] = _try(lambda seg=seg: _te_fx_types(seg))
                    out['segments'].append(row)
                    for fx_type in row['effects'] or []:
                        if fx_type not in out['effects']:
                            out['effects'].append(fx_type)
    return out


def _ex_snapshot(seg, parent, dest):
    _tl_check_media(parent, 'the sequence')
    _dm_check_dest(dest)
    facts = _tl_seg_facts(seg)
    if facts['type'] == 'Gap':
        raise _OpError('PRECONDITION_UNMET', 'the segment is a Gap: there is no media to take out')
    sequence = _dc_with_selector(parent)
    return {
        'source': {
            'segment': facts,
            'sequence': sequence,
            'location': _tl_location(parent),
            'effects': _te_fx_types(seg),
        },
        'destination': _dc_with_selector(dest),
        'existing': _dc_rows(_dm_media_children(dest)),
        'error': None,
        'errorCode': None,
    }


def _ex_call(seg, dest, mode, preserve_handle, include_fx):
    if mode == 'copy':
        return seg.copy_to_media_panel(dest)
    kwargs = {}
    if preserve_handle:
        kwargs['preserve_handle'] = True
    if include_fx:
        kwargs['include_timeline_fx'] = True
    return seg.match(dest, **kwargs)


def _ex_answer(returned):
    if isinstance(returned, (list, tuple)):
        return list(returned), 'list'
    if returned is None:
        return [], 'none'
    return [returned], 'single'


def _ex_run(seg, parent, dest, mode, preserve_handle, include_fx, expected):
    snapshot = _ex_snapshot(seg, parent, dest)
    before = _dm_media_children(dest)
    before_uids = [_dm_uid(o) for o in before]
    out = {
        'mode': mode,
        'preserveHandle': preserve_handle,
        'includeTimelineFx': include_fx,
        'source': snapshot['source'],
        'destination': snapshot['destination'],
        'returned': None,
        'created': None,
        'clip': None,
        'state': None,
        'destinationEntriesBefore': _dc_names(before),
        'destinationEntriesAfter': None,
        'error': None,
        'errorCode': None,
        'warnings': [],
    }
    if _dm_changed(before_uids, expected):
        out['error'] = (
            'nothing was taken out: the destination changed between the read and the write'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    got, shape = _ex_answer(_ex_call(seg, dest, mode, preserve_handle, include_fx))
    out['returned'] = {'shape': shape, 'count': len(got), 'types': [type(o).__name__ for o in got]}
    try:
        after = _dm_media_children(dest)
        out['destinationEntriesAfter'] = _dc_names(after)
        new = _dm_new_entries(before_uids, after)
        if not new and type(dest).__name__ != 'PyReel':
            new = _dm_unlisted(got, before_uids)
            if new:
                out['warnings'].append(_dm_unlisted_warning(dest))
        if not new:
            out['error'] = (
                'the call answered ' + str(len(got)) + ' object(s) (' + shape + ') and the '
                'destination did not grow (had ' + repr(out['destinationEntriesBefore'])
                + ', now ' + repr(out['destinationEntriesAfter']) + ')'
            )
            out['errorCode'] = 'SILENT_NOOP'
            return out
        made = _tl_pick_new(new, got)
        if made is None:
            out['candidates'] = [_dc_with_selector(o) for o in new]
            out['error'] = (
                str(len(new)) + ' new entries appeared in the destination and none of them can '
                'be told to be the clip (names ' + repr(_dc_names(new)) + '): nothing is '
                'recorded as made, so nothing can be reverted'
            )
            out['errorCode'] = 'FLAME_API_ERROR'
            return out
        out['created'] = _dc_with_selector(made)
        out['clip'] = _ex_clip_facts(made)
        out['state'] = _ex_state(made)
        if len(new) > 1:
            out['warnings'].append(
                str(len(new)) + ' new entries appeared; the one Flame answered is taken'
            )
    except BaseException as exc:
        if out['error'] is None:
            out['error'] = (
                'the clip was made but reading it back failed: ' + type(exc).__name__ + ': '
                + str(exc)
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _ex_state_problems(baseline, current):
    problems = []
    for key in ('name', 'duration', 'fingerprint'):
        if key in baseline and baseline[key] != current.get(key):
            problems.append(
                key + ' is ' + repr(current.get(key)) + ' now, it was ' + repr(baseline[key])
                + ' when the clip was made'
            )
    return problems


def _ex_revert(created):
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
    state = _ex_state(obj)
    out['currentState'] = state
    problems = _ex_state_problems(created.get('state') or {}, state)
    if problems:
        out['conflicts'] = problems
        out['error'] = (
            'nothing was deleted: the clip has changed since it was made (' + '; '.join(problems)
            + ')'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    gone, returned, still = _dm_delete(obj, uid, created.get('parent'))
    out['deleteReturned'] = _ja(returned)
    out['deleted'] = gone
    if not gone:
        out['error'] = (
            'flame.delete returned ' + repr(_ja(returned)) + ' and the clip is '
            + ('still there' if still is not None else 'gone')
        )
        out['errorCode'] = 'SILENT_NOOP'
    return out
"""
