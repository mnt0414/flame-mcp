"""Prelude source for the segment connection ops (`ops/segment_connect.py`): `segment.connections`,
`segment.create_connection`, `segment.remove_connection` and `segment.sync_connections`, and for
the connected-segment notices of `segment.replace` and `timeline.delete_segments`.

Like `timeline_edit_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`,
`_describe_entry`, `_wiretap_selector` and the `_tl_*` / `_te_*` / `_rp_*` helpers already defined.

What it encodes (Flame 2025.2 Python API; measured on Flame 2025.2.4):
  - `PySegment.connected_segments()` is a METHOD that answers a list of PySegment;
    `create_connection()`, `remove_connection()` and `sync_connected_segments()` are methods too.
  - once segments are connected (the UI's "Create Segment Connection"), replacing the media of one
    of them (`smart_replace_media` / `smart_replace`) also replaces the media of the segments
    connected to it, in OTHER sequences. A Timeline checkpoint (and so `journal.revert`) puts back
    only the sequence the op targeted, never those. Whether `flame.delete` of a segment deletes the
    segments connected to it was NOT measured.
  - `connected_segments()` lists the segment itself: a connected pair answers 2 for each of them,
    and a segment that is not connected answers 1, i.e. `[self]` (measured on a track-0 video
    segment). So the connected count that is reported is the number of OTHER segments:
    `_sc_others` leaves out the one that is the target, found the way `segment.connections`
    finds `isTarget` (its position in its container), else one is taken off the length.
  - `timeline.delete_segments` on a copy whose segments were connected to an original left the
    original sequence unchanged (segment count and positions read back), also after the op had
    deleted its extract by-product clips (measured).
  - a PySegment has no uid and no wiretap id: a connected segment is placed by walking its parents
    (PyTrack -> PyVersion -> PySequence / PyClip) to its container and counting the container's
    segments with the same record range, source name and source_in (`read_prelude._rp_shared_item`).
  - what the three write calls return was NOT recorded. The writes have no journal, so a count
    that did not move the expected way is a warning, never an error (a false SILENT_NOOP is the
    worst case); the inverse call named in the result is presumed, not measured.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

CONNECTION_HELPER = """
_SC_CALLS = {
    'create': 'create_connection',
    'remove': 'remove_connection',
    'sync': 'sync_connected_segments',
}
_SC_INVERSE = {'create': 'segment.remove_connection', 'remove': 'segment.create_connection'}
_SC_AFFECTS = (
    'The connections of this segment may reach segments in other sequences: this op leaves no '
    'journal record and no checkpoint, and journal.revert does not undo it. result.inverse is '
    'a presumed inverse call'
)
_SC_UNMEASURED = (
    'connected_segments() lists the segment itself (measured), so the count leaves it out'
)


def _sc_others(seg, parent, version, track, cache):
    # (the number of OTHER segments connected_segments() answers, an error text); the count is
    # None when the call raised. The call lists the segment itself too (measured: a connected pair
    # answers 2, a segment with no connection answers [self]), so the entry that is the target is
    # left out: found by its position in its container, as segment.connections finds isTarget.
    # When none can be told to be the target, one is taken off the length.
    try:
        listed = list(seg.connected_segments())
        tsel = _wiretap_selector(parent)
        target_id = None if tsel is None else tsel['id']
        target_pos = _rp_position(parent, version, track, seg)
        flagged = 0
        if target_pos is not None:
            for s in listed:
                if _rp_shared_item(s, target_id, target_pos, cache)['isTarget']:
                    flagged += 1
        return len(listed) - (flagged if flagged else min(len(listed), 1)), None
    except BaseException as exc:
        return None, type(exc).__name__ + ': ' + str(exc)


def _sc_replace_notice(count):
    return (
        str(count) + ' segment(s) are connected to this one (PySegment.connected_segments()): the '
        'replacement is propagated to them in other sequences, and journal.revert (a restore of '
        'this sequence only) does not put them back'
    )


def _sc_item(seg, target_id, target_pos, cache):
    # one connected segment: the sequence it is in, where in it, and a segment selector when its
    # position is unique. Whatever cannot be resolved is None; `error` says why.
    base = _rp_shared_item(seg, target_id, target_pos, cache)
    out = {
        'sequence': None,
        'uid': None,
        'wiretapId': None,
        'version': None,
        'track': None,
        'index': None,
        'name': _rp_try(lambda: _v(seg.name)),
        'sourceName': base['sourceName'],
        'recordIn': base['recordIn'],
        'recordOut': base['recordOut'],
        'isTarget': base['isTarget'],
        'selector': base['selector'],
        'ambiguous': base['ambiguous'],
    }
    try:
        holder = seg.parent.parent.parent
        entry = _describe_entry(holder)
        out['sequence'] = {'type': entry['type'], 'name': entry['name']}
        out['uid'] = entry['uid']
        out['wiretapId'] = entry['wiretapId']
    except BaseException as exc:
        out['error'] = type(exc).__name__ + ': ' + str(exc)
    if base['selector'] is not None:
        out['version'] = base['selector']['version']
        out['track'] = base['selector']['track']
        out['index'] = base['selector']['index']
    if base.get('candidates') is not None:
        out['candidates'] = base['candidates']
    if base.get('error') is not None and 'error' not in out:
        out['error'] = base['error']
    return out


def _sc_connections(seg, parent, version, track, limit, offset):
    # the OTHER segments connected to the target: connected_segments() lists the target itself
    # (measured), and the entry that is the target is left out (`targetListed` says whether it was
    # found; when it was not, `count` may include it and a warning says so)
    _tl_check_media(parent, 'the sequence')
    listed = list(seg.connected_segments())
    tsel = _wiretap_selector(parent)
    target_id = None if tsel is None else tsel['id']
    target_pos = _rp_position(parent, version, track, seg)
    cache = {}
    every = [_sc_item(s, target_id, target_pos, cache) for s in listed]
    others = [item for item in every if not item['isTarget']]
    target_listed = len(others) < len(every)
    warnings = []
    if listed and not target_listed:
        warnings.append(
            'the target could not be told apart in the list connected_segments() answered (its '
            'position in the sequence is not unique): count may include the target itself'
        )
    items = others[offset:offset + limit]
    return {
        'count': len(others),
        'total': len(others),
        'offset': offset,
        'shown': len(items),
        'truncated': offset + len(items) < len(others),
        'targetListed': target_listed,
        'target': _tl_seg_facts(seg),
        'items': items,
        'warnings': warnings,
    }


def _sc_write_check(seg, parent, action, version, track):
    out = _te_start(seg, parent, 'change the connections of', True)
    out['action'] = action
    if out['error'] is not None:
        return out
    count, problem = _sc_others(seg, parent, version, track, {})
    out['connected'] = {'before': count, 'after': None}
    if count is None:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'connected_segments() raised ' + problem + ': nothing was changed',
        )
    return out


def _sc_judge(out, action, call, before, after):
    # create should leave more connected segments than before, remove fewer; sync is not judged.
    # A count that did not move that way is a warning: nothing here is undone by a journal, so a
    # false error is worse than a missing one.
    moved = (action == 'create' and after > before) or (action == 'remove' and after < before)
    if action == 'sync' or moved:
        return out
    out['warnings'].append(
        call + '() returned ' + repr(out['returned']) + ' and connected_segments() answers '
        + str(after) + ' segment(s) (it answered ' + str(before) + ' before): whether the '
        'connection changed is not known; ' + _SC_UNMEASURED
    )
    return out


def _sc_write(seg, parent, version, track, action):
    out = _sc_write_check(seg, parent, action, version, track)
    if out['error'] is not None:
        return out
    record_in = out['before']['recordIn']['frame']
    call = _SC_CALLS[action]
    out.update(
        ran=True,
        after=None,
        returned=None,
        position=None,
        inverse=None,
        notices=[_SC_AFFECTS],
        warnings=[],
    )
    out['returned'] = _ja(getattr(seg, call)())
    out['position'] = _te_position(parent, version, track, record_in)
    index, found = _tl_find_at(parent, version, track, record_in)
    out['after'] = None if found is None else _tl_seg_facts(found)
    count, problem = _sc_others(found if found is not None else seg, parent, version, track, {})
    out['connected']['after'] = count
    if count is None:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            call + '() returned ' + repr(out['returned']) + ' but connected_segments() raised '
            + problem + ': what it did is not known',
        )
    out = _sc_judge(out, action, call, out['connected']['before'], count)
    if not out['warnings'] and action in _SC_INVERSE and out['position'] is not None:
        out['inverse'] = {
            'op': _SC_INVERSE[action],
            'args': {'target': out['position']['selector']},
        }
    return out
"""
