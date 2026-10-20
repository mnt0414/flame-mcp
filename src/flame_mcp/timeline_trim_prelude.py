"""Prelude source for `plan.run`'s up-front resolution of a bundled plan's segment selectors and for
`segment.trim` (`ops/segment_trim.py`, `plan_timeline.py`).

Like `timeline_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_OpError`, `_segment_list`,
`_wiretap_selector` and the `_tl_*` helpers of `timeline_prelude` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `trim_head(n)` / `trim_tail(n)`: n > 0 shortens, n < 0 extends (as far as the handle goes); the
    return value False does not mean failure. Without ripple a head trim moves record_in by n and
    leaves a gap before the segment; with `ripple=True` only the same track is closed up, the other
    tracks do not follow. Segment edits run on the main thread only.
  - the facts / track-row / position helpers here (`_tl_seg_facts`, `_tl_track_rows`,
    `_tl_position`, `_tl_find_at`, `_tl_deltas`) are what the other segment edits
    (`timeline_edit_prelude`) judge by too.
  - a segment with a Timewarp effect whose source is not as long as its record (made in the UI,
    its setup then loaded onto test segments): Flame keeps its source range at the speed (44
    record frames show 35 source frames at 0.8), so a trim or a slip moves source_in / source_out
    by the speed times the change, rounded (0.8: trim +-3 -> source +-2; slip +-2 -> source_in -+2,
    source_out -+1 or -+2), while the record side, head and tail move 1:1 as without it. The
    rounding is not known, so for such a segment the source side is not judged
    (`_tl_judge_source`): the record side, head and tail still are, and the result says
    `timewarp`, `sourceJudged: false`, `sourceNotJudgedBecause` and shows the deltas as read back.
    A Timewarp whose source is as long as the record (made through the API, 100%, bypassed) is
    judged 1:1 like a segment without one.
"""

from __future__ import annotations

TRIM_HELPER = """

def _tl_pre_row(get_parent, late, version, track, index, record_in, expect):
    # One write step's selector, resolved without writing: the sequence / clip it belongs to (what a
    # plan bundles on) and, unless `late`, the segment itself (index, record_in, source_name, as
    # the same `_resolve_*` an op's own code runs). Answers {ok, errorCode, error, target, segment}.
    out = {'ok': False, 'errorCode': None, 'error': None, 'target': None, 'segment': None}
    try:
        parent = get_parent()
        _tl_check_media(parent, 'the sequence')
        refusal = _tl_refuse_location(parent, 'the sequence')
        if refusal is not None:
            raise _OpError('PRECONDITION_UNMET', refusal)
        out['target'] = _tl_ident(parent)
        if not late and (index is not None or record_in is not None):
            if index is not None:
                seg = _resolve_segment(parent, version, track, index, expect)
                found = index
            else:
                seg = _resolve_segment_at(parent, version, track, record_in, expect)
                found = _tl_find_at(parent, version, track, record_in)[0]
            out['segment'] = {
                'index': found,
                'recordIn': seg.record_in.frame,
                'sourceName': _v(seg.source_name),
                'type': _j(seg.type),
            }
        out['ok'] = True
    except _OpError as exc:
        out['errorCode'] = exc.code
        out['error'] = exc.message
    except BaseException as exc:
        out['errorCode'] = 'FLAME_API_ERROR'
        out['error'] = type(exc).__name__ + ': ' + str(exc)
    return out


def _tl_seg_facts(seg):
    return {
        'type': _j(seg.type),
        'sourceName': _v(seg.source_name),
        'recordIn': _j(seg.record_in),
        'recordOut': _j(seg.record_out),
        'sourceIn': _j(seg.source_in),
        'sourceOut': _j(seg.source_out),
        'recordDuration': _j(seg.record_duration),
        'head': _j(seg.head),
        'tail': _j(seg.tail),
    }


def _tl_seg_numbers(seg):
    return {
        'recordIn': seg.record_in.frame,
        'recordOut': seg.record_out.frame,
        'sourceIn': seg.source_in.frame,
        'sourceOut': seg.source_out.frame,
        'head': _j(seg.head),
        'tail': _j(seg.tail),
    }


def _tl_track_rows(parent, version, track):
    rows = []
    for seg in _segment_list(parent, version, track):
        n = _tl_seg_numbers(seg)
        rows.append(
            [
                _j(seg.type),
                n['recordIn'],
                n['recordOut'],
                n['sourceIn'],
                n['sourceOut'],
                n['head'],
                n['tail'],
            ]
        )
    return rows


def _tl_track_summary(parent, version, track):
    segs = _segment_list(parent, version, track)
    if not segs:
        return {
            'track': track,
            'segmentCount': 0,
            'startFrame': None,
            'endFrame': None,
            'trailingGap': None,
        }
    last = segs[-1]
    gap = None
    if _j(last.type) == 'Gap':
        gap = {'recordIn': last.record_in.frame, 'recordOut': last.record_out.frame}
    return {
        'track': track,
        'segmentCount': len(segs),
        'startFrame': segs[0].record_in.frame,
        'endFrame': max(s.record_out.frame for s in segs),
        'trailingGap': gap,
    }


def _tl_track_ends(parent, version):
    ends = []
    for ti in range(len(list(list(parent.versions)[version].tracks))):
        ends.append({'track': ti, 'endFrame': _tl_track_summary(parent, version, ti)['endFrame']})
    return ends


def _tl_trim_check(seg, parent, edge, offset):
    out = {'ran': False, 'before': None, 'handle': None, 'error': None, 'errorCode': None}
    if edge not in ('head', 'tail'):
        out['error'] = 'edge must be "head" or "tail", got ' + repr(edge)
        out['errorCode'] = 'INVALID_ARGS'
        return out
    _tl_check_media(parent, 'the sequence')
    refusal = _tl_refuse_location(parent, 'the sequence')
    if refusal is not None:
        out['error'] = refusal
        out['errorCode'] = 'PRECONDITION_UNMET'
        return out
    out['before'] = _tl_seg_facts(seg)
    if out['before']['type'] == 'Gap':
        out['error'] = 'the segment is a Gap: there is no media to trim'
        out['errorCode'] = 'PRECONDITION_UNMET'
        return out
    _tl_judge_source(seg, out)
    if out['error'] is not None:
        return out
    handle = out['before']['head'] if edge == 'head' else out['before']['tail']
    out['handle'] = handle
    if offset < 0:
        if handle != 'infinite' and (not isinstance(handle, int) or -offset > handle):
            out['error'] = (
                'cannot extend the ' + edge + ' by ' + str(-offset) + ' frame(s): the ' + edge
                + ' handle is ' + repr(handle) + ' (offset -' + str(handle) + ' is the most)'
            )
            out['errorCode'] = 'PRECONDITION_UNMET'
        return out
    duration = out['before']['recordDuration']['frame']
    if offset >= duration:
        out['error'] = (
            'cannot trim ' + str(offset) + ' frame(s) off a segment of ' + str(duration)
            + ' frame(s): it would leave nothing'
        )
        out['errorCode'] = 'PRECONDITION_UNMET'
    return out


def _tl_expected_deltas(edge, offset, ripple):
    if edge == 'head':
        return {
            'recordIn': 0 if ripple else offset,
            'recordOut': -offset if ripple else 0,
            'sourceIn': offset,
            'sourceOut': 0,
            'head': offset,
            'tail': 0,
        }
    return {
        'recordIn': 0,
        'recordOut': -offset,
        'sourceIn': 0,
        'sourceOut': -offset,
        'head': 0,
        'tail': offset,
    }


def _tl_deltas(before, after):
    out = {}
    for key in ('recordIn', 'recordOut', 'sourceIn', 'sourceOut', 'head', 'tail'):
        b, a = before[key], after[key]
        out[key] = a - b if isinstance(a, int) and isinstance(b, int) else (0 if a == b else None)
    return out


def _tl_has_timewarp(seg):
    return any(_j(fx.type) == 'Timewarp' for fx in list(seg.effects))


def _tl_judge_source(seg, out):
    # Says in `out` whether the source side is judged: not when the segment has a Timewarp effect
    # and its source is not as long as its record. Effects that cannot be read stop the
    # edit before anything is written.
    try:
        timewarp = _tl_has_timewarp(seg)
    except BaseException as exc:
        out['error'] = (
            'cannot read the effects of the segment (' + type(exc).__name__ + ': ' + str(exc)
            + '): nothing was written'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return
    before = out['before']
    source_length = before['sourceOut']['frame'] - before['sourceIn']['frame'] + 1
    record_length = before['recordDuration']['frame']
    unjudged = timewarp and source_length != record_length
    out['timewarp'] = timewarp
    out['sourceJudged'] = not unjudged
    out['sourceNotJudgedBecause'] = (
        'Timewarp, source length ' + str(source_length) + ' != record length ' + str(record_length)
        if unjudged else None
    )


def _tl_expected_shown(expected, source_judged):
    # the source side of an unjudged segment has no expected value (None)
    if source_judged:
        return expected
    return dict(expected, sourceIn=None, sourceOut=None)


def _tl_deltas_match(deltas, expected, source_judged):
    for key in ('recordIn', 'recordOut', 'sourceIn', 'sourceOut', 'head', 'tail'):
        if not source_judged and key in ('sourceIn', 'sourceOut'):
            continue
        if deltas[key] != expected[key]:
            return False
    return True


def _tl_find_at(parent, version, track, record_in):
    for i, seg in enumerate(_segment_list(parent, version, track)):
        if seg.record_in.frame == record_in:
            return i, seg
    return None, None


def _tl_position(parent, version, track, index, seg):
    wsel = _wiretap_selector(parent)
    frame = seg.record_in.frame
    base = {'kind': 'segment', 'parent': wsel, 'version': version, 'track': track}
    name = _v(seg.source_name)
    if isinstance(name, str) and name != '':
        base['expect_source_name'] = name
    return {
        'version': version,
        'track': track,
        'index': index,
        'recordIn': frame,
        'selector': dict(base, record_in=frame),
        'selectorByIndex': dict(base, index=index),
    }


def _tl_trim(seg, parent, version, track, edge, offset, ripple):
    out = _tl_trim_check(seg, parent, edge, offset)
    if out['error'] is not None:
        return out
    before_rows = _tl_track_rows(parent, version, track)
    numbers = _tl_seg_numbers(seg)
    expected = _tl_expected_deltas(edge, offset, ripple)
    out.update(
        after=None,
        position=None,
        track=None,
        trackBefore=_tl_track_summary(parent, version, track),
        trackEnds=None,
        returned=None,
        deltas=None,
        expectedDeltas=_tl_expected_shown(expected, out['sourceJudged']),
        ran=True,
    )
    if edge == 'head':
        returned = seg.trim_head(offset, ripple=True) if ripple else seg.trim_head(offset)
    else:
        returned = seg.trim_tail(offset, ripple=True) if ripple else seg.trim_tail(offset)
    out['returned'] = _ja(returned)
    after_rows = _tl_track_rows(parent, version, track)
    out['track'] = _tl_track_summary(parent, version, track)
    out['trackEnds'] = _tl_track_ends(parent, version)
    if after_rows == before_rows:
        out['error'] = (
            'trim_' + edge + '(' + str(offset) + ') returned ' + repr(out['returned'])
            + ' and nothing in the track changed'
        )
        out['errorCode'] = 'SILENT_NOOP'
        return out
    what = (
        'a ' + edge + ' trim of ' + str(offset) + (' with ripple' if ripple else ' without ripple')
    )
    return _tl_trim_judge(out, parent, version, track, numbers, expected, after_rows, what)


def _tl_trim_judge(out, parent, version, track, numbers, expected, after_rows, what):
    # reads the trimmed segment back and judges it against `expected`
    new_in = numbers['recordIn'] + expected['recordIn']
    index, found = _tl_find_at(parent, version, track, new_in)
    if found is None:
        out['error'] = (
            'the track changed but no segment starts at record_in ' + str(new_in)
            + ' (where the trimmed segment should be): the track is now ' + repr(after_rows[:40])
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    out['after'] = _tl_seg_facts(found)
    out['deltas'] = _tl_deltas(numbers, _tl_seg_numbers(found))
    out['position'] = _tl_position(parent, version, track, index, found)
    if not _tl_deltas_match(out['deltas'], expected, out['sourceJudged']):
        out['error'] = (
            'the segment changed, but not the way ' + what + ' is measured to change it '
            '(expected ' + repr(out['expectedDeltas']) + ', read back ' + repr(out['deltas'])
            + (', source side not judged: ' + out['sourceNotJudgedBecause']
               if not out['sourceJudged'] else '') + ')'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
    return out
"""
