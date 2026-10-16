"""Prelude source for the Timeline construction ops (`ops/timeline_build.py`):
`timeline.overwrite`, `timeline.insert`, `timeline.cut`, `timeline.create_version`,
`timeline.create_track`.

Like `timeline_edit_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`,
`_segment_list`, `_wiretap_selector` and the `_tl_*` helpers of `timeline_prelude` /
`timeline_trim_prelude` already defined.

What it encodes (measured on Flame 2025.2.4, twice, identical):
  - a time is the absolute record frame, `flame.PyTime(frame, seq.frame_rate)` (`seq.frame_rate` is
    a str such as '23.976 fps'): overwrite / insert / cut all act at that frame whatever the
    sequence's start timecode is.
  - `seq.overwrite(clip, time, track)` / `seq.insert(clip, time, track)` put the clip's used range
    (the whole clip, or the range between its in / out marks, the out mark not included) at `time`
    in `track`; a time after the end of the track leaves a Gap in between; insert moves only the
    track it is given. Putting a clip into an empty sequence can change the sequence's start_frame.
  - `PyTrack.cut(time)` / `PySequence.cut(time)`: `time` becomes the first frame of the right
    part; a cut at the first frame of a segment, or outside the track's segments, changes nothing
    and raises nothing; a Gap is cut like a segment.
  - `seq.create_version()` adds a version at the end holding one track that is one Gap as long as
    the sequence; `version.create_track()` adds a track at the end, one Gap as long as the version.
  - the segments are judged by reading them back (`_tl_fingerprint` before and after): the
    fingerprint sees versions, tracks, segments and their effects, so an empty track counts.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

BUILD_HELPER = """
_TB_ROWS = 24
_TB_ROWS_MANY = 10
_TB_LEAD = 3
_TB_LISTED = 20


def _tb_fail(out, code, message):
    out['error'] = message
    out['errorCode'] = code
    return out


def _tb_new(extra):
    out = {'ran': False, 'before': None, 'error': None, 'errorCode': None}
    out.update(extra)
    return out


def _tb_sequence(parent, what):
    # (None, None) when `parent` is a sequence on the Desktop, else (code, message). A clip has no
    # overwrite / insert / cut / create_version of its own, a library or Batch one is not edited.
    kind = type(parent).__name__
    if kind != 'PySequence':
        return 'SELECTOR_TYPE', 'the target is a ' + kind + ', not a PySequence: ' + what
    refusal = _tl_refuse_location(parent, 'the sequence')
    if refusal is not None:
        return 'PRECONDITION_UNMET', refusal
    return None, None


def _tb_start(out, parent, what):
    code, message = _tb_sequence(parent, what)
    if code is not None:
        return _tb_fail(out, code, message)
    return out


def _tb_row(seg):
    return [_j(seg.type), seg.record_in.frame, seg.record_out.frame, _v(seg.source_name)]


def _tb_window(rows, at, size):
    if len(rows) <= size:
        return rows, False
    first = len(rows)
    if at is not None:
        for i, row in enumerate(rows):
            if row[2] >= at:
                first = i
                break
    else:
        first = 0
    start = max(0, min(first - _TB_LEAD, len(rows) - size))
    return rows[start:start + size], True


def _tb_track(seq, vi, ti, at, size, layout):
    rows = [_tb_row(s) for s in _segment_list(seq, vi, ti)]
    out = {'version': vi, 'track': ti, 'segmentCount': len(rows)}
    if rows:
        end = max(r[2] for r in rows)
        out['startFrame'] = rows[0][1]
        out['endFrame'] = end
        out['duration'] = end - rows[0][1] + 1
        last = rows[-1]
        out['trailingGap'] = (
            {'recordIn': last[1], 'recordOut': last[2]} if last[0] == 'Gap' else None
        )
    else:
        out['startFrame'] = None
        out['endFrame'] = None
        out['duration'] = 0
        out['trailingGap'] = None
    if layout:
        out['layout'], out['layoutWindowed'] = _tb_window(rows, at, size)
    return out


def _tb_state(seq, versions, layout_of, at):
    # what the sequence holds now: start_frame, duration, and for every track of `versions` (None
    # = all) its segment count, start, end (the last frame, inclusive), trailing Gap and, where
    # `layout_of(version, track)` says so, its segments around `at` as [type, record_in,
    # record_out, source_name]
    tracks = []
    for vi, ver in enumerate(list(seq.versions)):
        if versions is not None and vi not in versions:
            continue
        for ti in range(len(list(ver.tracks))):
            mode = layout_of(vi, ti) if layout_of is not None else 0
            size = _TB_ROWS if mode == 1 else _TB_ROWS_MANY
            tracks.append(_tb_track(seq, vi, ti, at, size, mode > 0))
    return {
        'startFrame': _try(lambda: _ja(seq.start_frame)),
        'duration': _try(lambda: _j(seq.duration)),
        'versionCount': len(list(seq.versions)),
        'tracks': tracks,
    }


def _tb_only(version, track):
    def layout_of(vi, ti):
        return 1 if (vi, ti) == (version, track) else 0

    return layout_of


def _tb_all(vi, ti):
    return 2


def _tb_time(seq, frame):
    return flame.PyTime(frame, seq.frame_rate)


def _tb_clip(clip):
    kind = type(clip).__name__
    if kind != 'PyClip':
        raise _OpError(
            'SELECTOR_TYPE',
            'source is a ' + kind + ', not a PyClip (a sequence or a reel is not put in)',
        )
    return _dc_with_selector(clip)


def _tb_put_check(parent, version, track, clip, at, mode):
    out = _tb_new({'mode': mode, 'at': at, 'source': None})
    if _tb_start(out, parent, mode + ' needs a sequence')['error'] is not None:
        return out
    _segment_list(parent, version, track)
    out['source'] = _tb_clip(clip)
    out['before'] = _tb_state(parent, [version], _tb_only(version, track), at)
    return out


def _tb_placed(parent, version, track, at):
    # the media segment that starts at `at` in the track, as the position the next edit can use
    index, found = _tl_find_at(parent, version, track, at)
    if found is None or _j(found.type) == 'Gap':
        return None
    return _tl_position(parent, version, track, index, found)


def _tb_put(parent, version, track, clip, at, mode):
    out = _tb_put_check(parent, version, track, clip, at, mode)
    if out['error'] is not None:
        return out
    fp_before = _tl_fingerprint(parent)['fingerprint']
    start_before = out['before']['startFrame']
    out.update(after=None, returned=None, changed=None, placed=None, startFrame=None,
               warnings=[], ran=True)
    trk = list(list(parent.versions)[version].tracks)[track]
    stamp = _tb_time(parent, at)
    if mode == 'overwrite':
        returned = parent.overwrite(clip, stamp, trk)
    else:
        returned = parent.insert(clip, stamp, trk)
    out['returned'] = {'type': type(returned).__name__}
    out['after'] = _tb_state(parent, [version], _tb_only(version, track), at)
    fp_after = _tl_fingerprint(parent)['fingerprint']
    start_after = out['after']['startFrame']
    out['startFrame'] = {'before': start_before, 'after': start_after,
                         'changed': start_before != start_after}
    out['changed'] = fp_after != fp_before
    if fp_after == fp_before and start_before == start_after:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            mode + '(' + repr(out['source']['name']) + ', at ' + str(at) + ') returned '
            + type(returned).__name__ + ' and nothing in the sequence changed',
        )
    out['placed'] = _tb_placed(parent, version, track, at)
    if out['placed'] is None:
        out['warnings'].append(
            'the sequence changed but no media segment starts at ' + str(at) + ' in track '
            + str(track) + ': read result.after.tracks to see where the clip went'
        )
    return out


def _tb_cuttable(parent, versions, tracks, at):
    # [{version, track, recordIn, recordOut}] of the media segments `at` falls inside
    # (record_in < at <= record_out): the only place a cut changes something that has media
    hits = []
    for vi, ver in enumerate(list(parent.versions)):
        if versions is not None and vi not in versions:
            continue
        for ti in range(len(list(ver.tracks))):
            if tracks is not None and ti not in tracks:
                continue
            for seg in _segment_list(parent, vi, ti):
                if _j(seg.type) != 'Gap' and seg.record_in.frame < at <= seg.record_out.frame:
                    hits.append({'version': vi, 'track': ti, 'recordIn': seg.record_in.frame,
                                 'recordOut': seg.record_out.frame})
    return hits


def _tb_cut_check(parent, version, track, at, scope):
    out = _tb_new({'scope': scope, 'at': at, 'willCut': None})
    if _tb_start(out, parent, 'cut needs a sequence')['error'] is not None:
        return out
    if scope == 'track':
        _segment_list(parent, version, track)
        hits = _tb_cuttable(parent, [version], [track], at)
        where = 'version ' + str(version) + ' track ' + str(track)
        out['before'] = _tb_state(parent, [version], _tb_only(version, track), at)
    else:
        hits = _tb_cuttable(parent, None, None, at)
        where = 'any track'
        out['before'] = _tb_state(parent, None, _tb_all, at)
    out['willCut'] = hits[:_TB_LISTED]
    if not hits:
        media = _tb_cuttable_ranges(parent, version, track, scope)
        out['mediaSegments'] = media
        return _tb_fail(
            out,
            'PRECONDITION_UNMET',
            'at ' + str(at) + ' is not inside a media segment of ' + where + ' (a cut needs '
            'record_in < at <= record_out of a segment that has media: at the first frame of a '
            'segment, in a Gap or outside the segments nothing happens)',
        )
    return out


def _tb_cuttable_ranges(parent, version, track, scope):
    rows = []
    for vi, ver in enumerate(list(parent.versions)):
        if scope == 'track' and vi != version:
            continue
        for ti in range(len(list(ver.tracks))):
            if scope == 'track' and ti != track:
                continue
            for seg in _segment_list(parent, vi, ti):
                if _j(seg.type) != 'Gap' and len(rows) < _TB_LISTED:
                    rows.append({'version': vi, 'track': ti, 'recordIn': seg.record_in.frame,
                                 'recordOut': seg.record_out.frame})
    return rows


def _tb_split_at(state, at, version, track):
    # did the track now have a segment ending at at - 1 and one starting at `at`
    for t in state['tracks']:
        if t['version'] != version or t['track'] != track:
            continue
        rows = t.get('layout') or []
        ends = [r for r in rows if r[2] == at - 1]
        starts = [r for r in rows if r[1] == at]
        return bool(ends) and bool(starts)
    return False


def _tb_cut(parent, version, track, at, scope):
    out = _tb_cut_check(parent, version, track, at, scope)
    if out['error'] is not None:
        return out
    fp_before = _tl_fingerprint(parent)['fingerprint']
    out.update(after=None, returned=None, changed=None, split=None, warnings=[], ran=True)
    stamp = _tb_time(parent, at)
    if scope == 'track':
        trk = list(list(parent.versions)[version].tracks)[track]
        returned = trk.cut(stamp)
    else:
        returned = parent.cut(stamp)
    out['returned'] = {'type': type(returned).__name__}
    if scope == 'track':
        out['after'] = _tb_state(parent, [version], _tb_only(version, track), at)
    else:
        out['after'] = _tb_state(parent, None, _tb_all, at)
    fp_after = _tl_fingerprint(parent)['fingerprint']
    out['changed'] = fp_after != fp_before
    if not out['changed']:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            'cut at ' + str(at) + ' (' + scope + ') returned ' + type(returned).__name__
            + ' and nothing in the sequence changed',
        )
    if scope == 'track':
        out['split'] = _tb_split_at(out['after'], at, version, track)
        if not out['split']:
            out['warnings'].append(
                'the track changed but no segment ends at ' + str(at - 1) + ' beside one that '
                'starts at ' + str(at) + ': read result.after.tracks'
            )
    else:
        cut = []
        for hit in out['willCut']:
            for t in out['after']['tracks']:
                same = (t['version'], t['track']) == (hit['version'], hit['track'])
                if same and t.get('layout') is not None:
                    rows = t['layout']
                    if [r for r in rows if r[2] == at - 1] and [r for r in rows if r[1] == at]:
                        cut.append({'version': t['version'], 'track': t['track']})
        out['split'] = cut
    return out


def _tb_version_counts(seq):
    return [len(list(ver.tracks)) for ver in list(seq.versions)]


def _tb_create_version_check(parent):
    out = _tb_new({'versionsBefore': None})
    if _tb_start(out, parent, 'create_version needs a sequence')['error'] is not None:
        return out
    out['before'] = _tb_state(parent, None, None, None)
    out['versionsBefore'] = _tb_version_counts(parent)
    return out


def _tb_create_version(parent):
    out = _tb_create_version_check(parent)
    if out['error'] is not None:
        return out
    fp_before = _tl_fingerprint(parent)['fingerprint']
    out.update(after=None, returned=None, changed=None, version=None, versionsAfter=None,
               warnings=[], ran=True)
    returned = parent.create_version()
    out['returned'] = {'type': type(returned).__name__}
    out['after'] = _tb_state(parent, None, None, None)
    out['versionsAfter'] = _tb_version_counts(parent)
    fp_after = _tl_fingerprint(parent)['fingerprint']
    out['changed'] = fp_after != fp_before
    if not out['changed'] or len(out['versionsAfter']) == len(out['versionsBefore']):
        return _tb_fail(
            out,
            'SILENT_NOOP',
            'create_version() returned ' + type(returned).__name__ + ' and the sequence has '
            + str(len(out['versionsAfter'])) + ' version(s), as before',
        )
    index = len(out['versionsAfter']) - 1
    out['version'] = {
        'index': index,
        'tracks': [t for t in out['after']['tracks'] if t['version'] == index],
    }
    if len(out['versionsAfter']) != len(out['versionsBefore']) + 1:
        out['warnings'].append(
            'the sequence had ' + str(len(out['versionsBefore'])) + ' version(s) and now has '
            + str(len(out['versionsAfter']))
        )
    return out


def _tb_create_track_check(parent, version):
    out = _tb_new({'tracksBefore': None})
    if _tb_start(out, parent, 'create_track needs a sequence')['error'] is not None:
        return out
    versions = list(parent.versions)
    if version >= len(versions):
        raise _OpError(
            'SELECTOR_EMPTY', 'version index ' + str(version) + ' out of range (have '
            + str(len(versions)) + ')'
        )
    out['before'] = _tb_state(parent, [version], None, None)
    out['tracksBefore'] = len(list(versions[version].tracks))
    return out


def _tb_create_track(parent, version):
    out = _tb_create_track_check(parent, version)
    if out['error'] is not None:
        return out
    fp_before = _tl_fingerprint(parent)['fingerprint']
    out.update(after=None, returned=None, changed=None, track=None, tracksAfter=None,
               warnings=[], ran=True)
    ver = list(parent.versions)[version]
    returned = ver.create_track()
    out['returned'] = {'type': type(returned).__name__}
    out['after'] = _tb_state(parent, [version], None, None)
    out['tracksAfter'] = len(list(list(parent.versions)[version].tracks))
    fp_after = _tl_fingerprint(parent)['fingerprint']
    out['changed'] = fp_after != fp_before
    if not out['changed'] or out['tracksAfter'] == out['tracksBefore']:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            'create_track() returned ' + type(returned).__name__ + ' and version '
            + str(version) + ' has ' + str(out['tracksAfter']) + ' track(s), as before',
        )
    index = out['tracksAfter'] - 1
    out['track'] = {'index': index, 'summary': [t for t in out['after']['tracks']
                                                  if t['track'] == index][0]}
    if out['tracksAfter'] != out['tracksBefore'] + 1:
        out['warnings'].append(
            'version ' + str(version) + ' had ' + str(out['tracksBefore']) + ' track(s) and now '
            'has ' + str(out['tracksAfter'])
        )
    return out
"""
