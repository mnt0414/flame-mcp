"""Prelude source for `timeline.usage` (`ops/timeline_usage.py`).

Like `inspect_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_try`, `_rp_try`,
`_rp_segment_source`, `_wiretap_selector`, `_ip_walk`, `_ip_item` and `_OpError` already defined,
and it only reads.

What `_tu_run(report, scope, given, offset, max_n, budget_s, limit, item_offset)` does:
  - the sequences are `given` (every selector the op resolved, the same sequence once), or the
    PySequence entries of `_ip_walk(scope, ...)` (a closed library is never opened by the walk,
    Batch groups are not read); `offset` and `max_n` pick the page of them. The items come back
    paged by `limit` and `item_offset`, with `total` counting all of them.
  - only the video tracks (`PyVersion.tracks`) of every version are read; a Gap is left out.
  - the time budget counts from the start of the call (the walk included). Before each sequence but
    the first it is checked; past it the scan stops, `stoppedEarly` and `nextSequenceOffset` say so.
  - an attribute that cannot be read raises, and the sequence it is in is an unreadable one
    (`sequenceErrors`; 'unreferenced' does not judge). A segment that really names no source is only
    counted (`segmentsWithoutKey`).
  - 'unreferenced' collects only the identities of every sequence of the project in this one call
    and judges only when that was complete (see `_tu_unreferenced`); a clip is referenced when any
    one of its identities is in a scanned segment.

The judgements the real Flame may change are these functions, and nothing else makes a pair:
  - `_tu_idents(seg)`: every (kind, value) identity of a segment's source.
  - `_tu_key(seg)`: the one (key, matchedBy) the segments of a `shared` item are grouped by.
  - `_tu_is_unlinked(seg)`: whether a segment's media is offline.
"""

from __future__ import annotations

from typing import Final

USAGE_MAX_USES: Final[int] = 20
"""`uses` of a shared item stops at this many (`count` keeps counting)."""
USAGE_MAX_SCANNED_LIST: Final[int] = 500
"""`scannedSequences` stops at this many rows."""

USAGE_HELPER = (
    f"_TU_MAX_USES = {USAGE_MAX_USES}\n_TU_MAX_SCANNED = {USAGE_MAX_SCANNED_LIST}\n"
    + """

def _tu_text(seg, name):
    # an attribute that cannot be read raises: the caller takes the sequence as unreadable
    value = _v(getattr(seg, name, None))
    return value if isinstance(value, str) else ''


def _tu_idents(seg):
    # every identity of the segment's source, most reliable first: [(kind, value)], [] when it
    # names none. The only place a (kind, value) pair is made.
    found = []
    path = _tu_text(seg, 'file_path')
    if path:
        found.append(('filePath', path))
    name = _tu_text(seg, 'source_name')
    if name:
        found.append(('sourceName', name))
    return found


def _tu_key(seg):
    # (key, matchedBy) the segments are grouped by, or None (it names no source)
    found = _tu_idents(seg)
    if not found:
        return None
    return (found[0][1], found[0][0])


def _tu_is_unlinked(seg):
    # 2025.2.4 reads '' when linked and 'V' (a string) when unlinked
    if _v(getattr(seg, 'source_unlinked', None)):
        return True
    kind = _v(seg.type)
    return isinstance(kind, str) and kind.startswith('Unlinked')


def _tu_ref(seq):
    return {'name': _try(lambda: _v(seq.name)), 'selector': _wiretap_selector(seq)}


def _tu_check_sequence(obj, label):
    kind = type(obj).__name__
    if kind != 'PySequence':
        raise _OpError('SELECTOR_TYPE', label + ' is a ' + kind + ', not a PySequence')
    return obj


def _tu_segments(seq):
    found = []
    for vi, ver in enumerate(list(seq.versions)):
        for ti, trk in enumerate(list(ver.tracks)):
            for si, seg in enumerate(list(trk.segments)):
                if _j(seg.type) == 'Gap':
                    continue
                found.append((vi, ti, si, seg))
    return found


def _tu_segment_selector(ref, vi, ti, frame):
    if ref['selector'] is None or not isinstance(frame, int):
        return None
    return {
        'kind': 'segment',
        'parent': ref['selector'],
        'version': vi,
        'track': ti,
        'record_in': frame,
    }


def _tu_where(ref, vi, ti, si, seg):
    record_in = _rp_try(lambda: _j(seg.record_in))
    frame = record_in.get('frame') if isinstance(record_in, dict) else None
    return {
        'sequence': ref,
        'version': vi,
        'track': ti,
        'index': si,
        'recordIn': record_in,
        'selector': _tu_segment_selector(ref, vi, ti, frame),
    }


def _tu_unlinked_item(ref, vi, ti, si, seg):
    src = _rp_segment_source(seg)
    where = _tu_where(ref, vi, ti, si, seg)
    return {
        'sequence': ref,
        'version': vi,
        'track': ti,
        'index': si,
        'recordIn': where['recordIn'],
        'type': _try(lambda: _j(seg.type)),
        'sourceName': _try(lambda: _v(seg.source_name)),
        'filePath': src['filePath'],
        'tapeName': src['tapeName'],
        'sourceUnlinked': src['sourceUnlinked'],
        'selector': where['selector'],
    }


def _tu_add_shared(groups, ref, vi, ti, si, seg):
    got = _tu_key(seg)
    if got is None:
        return
    group = groups.get(got)
    if group is None:
        src = _rp_segment_source(seg)
        group = {
            'key': got[0],
            'matchedBy': got[1],
            'sourceName': _try(lambda: _v(seg.source_name)),
            'filePath': src['filePath'],
            'count': 0,
            'uses': [],
            'usesTruncated': False,
        }
        groups[got] = group
    group['count'] += 1
    if len(group['uses']) < _TU_MAX_USES:
        group['uses'].append(_tu_where(ref, vi, ti, si, seg))
    else:
        group['usesTruncated'] = True


def _tu_over(started, budget_s):
    import time as _tu_time
    return _tu_time.monotonic() - started > budget_s


def _tu_scan_one(report, seq, ref, state):
    for vi, ti, si, seg in _tu_segments(seq):
        state['segments'] += 1
        idents = _tu_idents(seg)
        if not idents:
            state['nokey'] += 1
        if report == 'unlinked':
            if _tu_is_unlinked(seg):
                state['items'].append(_tu_unlinked_item(ref, vi, ti, si, seg))
        elif report == 'shared':
            _tu_add_shared(state['groups'], ref, vi, ti, si, seg)
        else:
            state['idents'].update(idents)


def _tu_scan(report, seqs, started, budget_s, state):
    # reads the sequences in order; stops before one (never before the first) when out of time
    read = 0
    for seq in seqs:
        if read > 0 and _tu_over(started, budget_s):
            return read, True
        ref = _tu_ref(seq)
        try:
            _tu_scan_one(report, seq, ref, state)
        except BaseException as exc:
            state['errors'].append({'sequence': ref, 'error': str(exc) or type(exc).__name__})
        state['refs'].append(ref)
        read += 1
    return read, False


def _tu_new_state():
    return {
        'segments': 0,
        'items': [],
        'groups': {},
        'idents': set(),
        'nokey': 0,
        'errors': [],
        'refs': [],
    }


def _tu_clip_source(clip):
    # (idents, key) of the segment inside a reel clip (version 0, track 0, index 0)
    seg = list(list(list(clip.versions)[0].tracks)[0].segments)[0]
    return _tu_idents(seg), _tu_key(seg)


def _tu_unique(seqs):
    # the same sequence given by selectors of different shapes is one sequence
    seen = set()
    out = []
    for seq in seqs:
        ident = _try(lambda: seq.get_wiretap_node_id())
        if not ident:
            ident = _try(lambda: _v(seq.uid))
        if not ident:
            out.append(seq)
            continue
        if ident in seen:
            continue
        seen.add(ident)
        out.append(seq)
    return out


def _tu_common(out, state):
    out['scannedSequences'] = state['refs'][:_TU_MAX_SCANNED]
    if len(state['refs']) > _TU_MAX_SCANNED:
        out['scannedSequencesTruncated'] = True
    out['segmentsScanned'] = state['segments']
    out['segmentsWithoutKey'] = state['nokey']
    if state['errors']:
        out['sequenceErrors'] = state['errors']


def _tu_walk_notes(out, walk):
    out['closedLibraries'] = walk['closedLibraries']
    if walk['stoppedEarly'] is not None:
        out['stoppedEarly'] = walk['stoppedEarly']
    unreadable = [
        {'path': e['path'], 'error': e['error']} for e in walk['entries'] if 'error' in e
    ]
    if unreadable:
        out['walkErrors'] = unreadable
    return unreadable


def _tu_not_judged(out, reason):
    out['judged'] = False
    out['reason'] = reason
    out['items'] = []


def _tu_page_item(entry, key):
    info = _ip_item(entry)
    return {
        'clip': {
            'type': info.get('type'),
            'name': info.get('name'),
            'uid': info.get('uid'),
            'selector': info.get('selector'),
            'path': info.get('path'),
        },
        'key': key[0],
        'matchedBy': key[1],
    }


def _tu_clip_candidates(walk, state, started, budget_s, out):
    # the clips none of whose identities is in a scanned segment; None when out of time
    found = []
    without_key = 0
    unreadable = 0
    read = 0
    for entry in walk['entries']:
        if 'obj' not in entry or type(entry['obj']).__name__ != 'PyClip':
            continue
        if _tu_over(started, budget_s):
            out['stoppedEarly'] = {
                'reason': 'time budget',
                'budgetS': budget_s,
                'read': out['judgedAgainst']['sequences'],
                'clipsRead': read,
            }
            return None
        read += 1
        try:
            idents, key = _tu_clip_source(entry['obj'])
        except BaseException:
            unreadable += 1
            continue
        if not idents:
            without_key += 1
        elif not any(i in state['idents'] for i in idents):
            found.append((entry, key))
    out['clipsWithoutKey'] = without_key
    if unreadable:
        out['clipsUnreadable'] = unreadable
    return found


def _tu_unreferenced(scope, given, budget_s, started, out, limit, item_offset):
    state = _tu_new_state()
    out['total'] = 0
    out['judgedAgainst'] = {
        'sequences': 0,
        'scope': scope,
        'closedLibrariesSkipped': [],
        'batchChecked': False,
        'segmentsWithoutKey': 0,
    }
    if scope != 'all':
        _tu_not_judged(out, 'scope')
        return out
    if given is not None:
        _tu_not_judged(out, 'sequences')
        return out
    walk = _ip_walk('all', ['PySequence', 'PyClip'], budget_s)
    unreadable = _tu_walk_notes(out, walk)
    out['judgedAgainst']['closedLibrariesSkipped'] = list(walk['closedLibraries'])
    if walk['closedLibraries']:
        _tu_not_judged(out, 'closedLibraries')
        return out
    if walk['stoppedEarly'] is not None:
        _tu_not_judged(out, 'budget')
        return out
    if unreadable:
        _tu_not_judged(out, 'unreadable')
        return out
    objs = [e['obj'] for e in walk['entries'] if 'obj' in e]
    seqs = [o for o in objs if type(o).__name__ == 'PySequence']
    read, stopped = _tu_scan('unreferenced', seqs, started, budget_s, state)
    _tu_common(out, state)
    out['judgedAgainst']['sequences'] = read
    out['judgedAgainst']['segmentsWithoutKey'] = state['nokey']
    if stopped:
        out['stoppedEarly'] = {'reason': 'time budget', 'budgetS': budget_s, 'read': read}
        _tu_not_judged(out, 'budget')
        return out
    if state['errors']:
        _tu_not_judged(out, 'unreadable')
        return out
    found = _tu_clip_candidates(walk, state, started, budget_s, out)
    if found is None:
        _tu_not_judged(out, 'budget')
        return out
    out['judged'] = True
    out['total'] = len(found)
    out['items'] = [_tu_page_item(e, k) for e, k in found[item_offset : item_offset + limit]]
    return out


def _tu_run(report, scope, given, offset, max_n, budget_s, limit, item_offset):
    import time as _tu_time
    started = _tu_time.monotonic()
    out = {
        'scannedSequences': [],
        'segmentsScanned': 0,
        'segmentsWithoutKey': 0,
        'closedLibraries': [],
        'batchUsageNotCounted': True,
    }
    if report == 'unreferenced':
        return _tu_unreferenced(scope, given, budget_s, started, out, limit, item_offset)
    if given is not None:
        seqs = _tu_unique(given)
    else:
        walk = _ip_walk(scope, ['PySequence'], budget_s)
        _tu_walk_notes(out, walk)
        seqs = [e['obj'] for e in walk['entries'] if 'obj' in e]
    total = len(seqs)
    state = _tu_new_state()
    read, stopped = _tu_scan(report, seqs[offset : offset + max_n], started, budget_s, state)
    _tu_common(out, state)
    if stopped and 'stoppedEarly' not in out:
        out['stoppedEarly'] = {'reason': 'time budget', 'budgetS': budget_s, 'read': read}
    if offset + read < total:
        out['nextSequenceOffset'] = offset + read
    if report == 'unlinked':
        found = state['items']
    else:
        found = [g for g in state['groups'].values() if g['count'] >= 2]
    out['total'] = len(found)
    out['items'] = found[item_offset : item_offset + limit]
    return out
"""
)
