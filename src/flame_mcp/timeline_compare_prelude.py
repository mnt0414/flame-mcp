"""Prelude source for `timeline.compare` (`ops/timeline.py`).

Like `read_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's own
code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_try`, `_OpError` and `_describe_entry`
already defined. It only reads.

What it compares (and what it does not): see the description of `timeline.compare`.
  - the video tracks of version 0: the number, and per track every segment as type, record range,
    source range, source_name, name and the effect type names;
  - the audio tracks (`PySequence.audio_tracks`), their channels and the segments of each channel
    (`channels[].segments` is the only way Flame lists an audio segment; measured on Flame 2025.2.4:
    type, source_name, record_in / record_out and source_in / source_out are readable): type,
    record range, source range, source_name;
  - the length, the version count, the frame rate and the size (`frame_rate`, `width`, `height`;
    one that cannot be read is a warning and is left out of the comparison).
  Record frames are made relative to each sequence's own start (`PySequence.start_time.frame`, what
  record_in values count from), so the same edit at another start timecode is equal; source frames
  stay as they are. What was measured is a sequence whose start_time is 0 (video and audio both
  start at record_in 0); a start_time that is not the first record_in of the video tracks, or an
  audio channel whose first record_in is not start_time, is a warning (the comparison goes on).

Two Gaps are compared by type, record range and effect types only: their source range and name
change without the edit changing (measured on Flame 2025.2.4: the same edit read from
two sequences gave source 177-192 against 90-105 and name '' against 'untitled'). A Gap against a
media segment is a difference as before.

Every difference is {where, field, a, b}; `where` counts from 0 like timeline.list_segments does.
"""

from __future__ import annotations

COMPARE_HELPER = """
_TC_VIDEO_FIELDS = (
    'type', 'recordIn', 'recordOut', 'sourceIn', 'sourceOut', 'sourceName', 'name', 'effects'
)
_TC_AUDIO_FIELDS = ('type', 'recordIn', 'recordOut', 'sourceIn', 'sourceOut', 'sourceName')
_TC_GAP_FIELDS = ('type', 'recordIn', 'recordOut', 'effects')
_TC_FACTS = (('frameRate', 'frame_rate'), ('width', 'width'), ('height', 'height'))


def _tc_row(seg, base, video):
    row = {
        'type': _j(seg.type),
        'recordIn': seg.record_in.frame - base,
        'recordOut': seg.record_out.frame - base,
        'sourceIn': seg.source_in.frame,
        'sourceOut': seg.source_out.frame,
        'sourceName': _v(seg.source_name),
    }
    if video:
        row['name'] = _try(lambda: _v(seg.name))
        row['effects'] = _try(lambda: [_j(fx.type) for fx in list(seg.effects)])
    return row


def _tc_read(obj, label):
    kind = type(obj).__name__
    if kind not in ('PySequence', 'PyClip'):
        raise _OpError(
            'SELECTOR_TYPE',
            label + ' is a ' + kind + ', not a PySequence or a PyClip',
        )
    base = _v(obj.start_time).frame
    versions = list(obj.versions)
    video = []
    warnings = []
    if versions:
        for trk in list(versions[0].tracks):
            video.append([_tc_row(s, base, True) for s in list(trk.segments)])
    firsts = [t[0]['recordIn'] for t in video if t]
    if firsts and min(firsts) != 0:
        warnings.append(
            label + ': start_time frame ' + str(base) + ' is not the first record_in of its video '
            'tracks (' + str(min(firsts) + base) + '); record frames are counted from start_time'
        )
    audio = []
    for atrk in list(obj.audio_tracks):
        audio.append([
            [_tc_row(s, base, False) for s in list(chan.segments)]
            for chan in list(atrk.channels)
        ])
    late = [
        'audio track ' + str(ai) + ' channel ' + str(ci) + ' (first record_in '
        + str(rows[0]['recordIn'] + base) + ')'
        for ai, chans in enumerate(audio)
        for ci, rows in enumerate(chans)
        if rows and rows[0]['recordIn'] != 0
    ]
    if late:
        warnings.append(
            label + ': the audio does not start at start_time frame ' + str(base) + ': '
            + ', '.join(late[:6]) + '; record frames are counted from start_time all the same'
        )
    facts = {}
    for key, attr in _TC_FACTS:
        try:
            facts[key] = _j(getattr(obj, attr))
        except BaseException as exc:
            facts[key] = None
            warnings.append(
                label + ': ' + attr + ' cannot be read (' + type(exc).__name__ + ': ' + str(exc)
                + '); it is not compared'
            )
    entry = _describe_entry(obj)
    return {
        'name': _try(lambda: _v(obj.name)),
        'uid': entry['uid'],
        'wiretapId': entry['wiretapId'],
        'startFrame': base,
        'duration': _v(obj.duration).frame,
        'versionCount': len(versions),
        'videoTrackCount': len(video),
        'audioTrackCount': len(audio),
        'frameRate': facts['frameRate'],
        'width': facts['width'],
        'height': facts['height'],
        'video': video,
        'audio': audio,
        'warnings': warnings,
    }


def _tc_note(ctx, scope, where, field, a, b):
    ctx['count'] += 1
    ctx['per'][scope] = ctx['per'].get(scope, 0) + 1
    if len(ctx['diffs']) < ctx['limit']:
        ctx['diffs'].append({'where': where, 'field': field, 'a': a, 'b': b})


def _tc_scope(ctx, scope):
    if scope not in ctx['per']:
        ctx['per'][scope] = 0
        ctx['order'].append(scope)


def _tc_segments(ctx, scope, rows_a, rows_b, fields):
    if len(rows_a) != len(rows_b):
        _tc_note(ctx, scope, scope, 'segmentCount', len(rows_a), len(rows_b))
    for j in range(max(len(rows_a), len(rows_b))):
        where = scope + ' segment ' + str(j)
        if j >= len(rows_a):
            _tc_note(ctx, scope, where, 'presence', 'absent', 'present')
        elif j >= len(rows_b):
            _tc_note(ctx, scope, where, 'presence', 'present', 'absent')
        else:
            both_gaps = rows_a[j]['type'] == 'Gap' and rows_b[j]['type'] == 'Gap'
            for field in fields:
                if both_gaps and field not in _TC_GAP_FIELDS:
                    continue
                if rows_a[j][field] != rows_b[j][field]:
                    _tc_note(ctx, scope, where, field, rows_a[j][field], rows_b[j][field])


def _tc_presence(ctx, scope, rows_a, rows_b):
    # one side has the track / channel and the other does not; True when both do
    if rows_a is not None and rows_b is not None:
        return True
    have = rows_a if rows_a is not None else rows_b
    text = 'present (' + str(len(have)) + ' segment(s))'
    _tc_note(ctx, scope, scope, 'presence', text if rows_a is not None else 'absent',
             text if rows_b is not None else 'absent')
    return False


def _tc_empty(read):
    return [i for i, rows in enumerate(read['video']) if all(r['type'] == 'Gap' for r in rows)]


def _tc_compare(a, b, limit):
    ctx = {'diffs': [], 'count': 0, 'limit': limit, 'per': {}, 'order': []}
    _tc_scope(ctx, 'sequence')
    for key in ('duration', 'versionCount', 'videoTrackCount', 'audioTrackCount'):
        if a[key] != b[key]:
            _tc_note(ctx, 'sequence', 'sequence', key, a[key], b[key])
    for key, attr in _TC_FACTS:
        if a[key] is not None and b[key] is not None and a[key] != b[key]:
            _tc_note(ctx, 'sequence', 'sequence', key, a[key], b[key])
    for i in range(max(len(a['video']), len(b['video']))):
        scope = 'video track ' + str(i)
        _tc_scope(ctx, scope)
        rows_a = a['video'][i] if i < len(a['video']) else None
        rows_b = b['video'][i] if i < len(b['video']) else None
        if _tc_presence(ctx, scope, rows_a, rows_b):
            _tc_segments(ctx, scope, rows_a, rows_b, _TC_VIDEO_FIELDS)
    for i in range(max(len(a['audio']), len(b['audio']))):
        track_scope = 'audio track ' + str(i)
        _tc_scope(ctx, track_scope)
        chans_a = a['audio'][i] if i < len(a['audio']) else None
        chans_b = b['audio'][i] if i < len(b['audio']) else None
        if chans_a is None or chans_b is None:
            have = chans_a if chans_a is not None else chans_b
            text = 'present (' + str(len(have)) + ' channel(s))'
            _tc_note(ctx, track_scope, track_scope, 'presence',
                     text if chans_a is not None else 'absent',
                     text if chans_b is not None else 'absent')
            continue
        if len(chans_a) != len(chans_b):
            _tc_note(ctx, track_scope, track_scope, 'channelCount', len(chans_a), len(chans_b))
        for j in range(max(len(chans_a), len(chans_b))):
            scope = track_scope + ' channel ' + str(j)
            _tc_scope(ctx, scope)
            rows_a = chans_a[j] if j < len(chans_a) else None
            rows_b = chans_b[j] if j < len(chans_b) else None
            if _tc_presence(ctx, scope, rows_a, rows_b):
                _tc_segments(ctx, scope, rows_a, rows_b, _TC_AUDIO_FIELDS)
    return {
        'equal': ctx['count'] == 0,
        'differences': ctx['diffs'],
        'differenceCount': ctx['count'],
        'truncated': ctx['count'] > len(ctx['diffs']),
        'summary': [
            {'where': s, 'equal': ctx['per'][s] == 0, 'differences': ctx['per'][s]}
            for s in ctx['order']
        ],
        'emptyTracks': {'a': _tc_empty(a), 'b': _tc_empty(b)},
        'a': _tc_side(a),
        'b': _tc_side(b),
        'warnings': a['warnings'] + b['warnings'],
    }


def _tc_side(read):
    return {
        'name': read['name'],
        'uid': read['uid'],
        'wiretapId': read['wiretapId'],
        'startFrame': read['startFrame'],
        'duration': read['duration'],
        'versionCount': read['versionCount'],
        'videoTrackCount': read['videoTrackCount'],
        'audioTrackCount': read['audioTrackCount'],
        'frameRate': read['frameRate'],
        'width': read['width'],
        'height': read['height'],
    }
"""
