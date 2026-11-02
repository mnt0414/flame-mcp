"""Prelude source for `desktop.sync_record_tc`, `desktop.get_record_tc` and
`desktop.set_record_tc` (`ops/desktop_tc.py`, `tc_sync_run.py`).

Real Python source that `execution.build_exec_code` places ahead of an op's own code. It runs inside
Flame (3.11) with `flame`, `_v`, `_j`, `_try`, `_OpError`, `_wiretap_selector`, `_stable_desktop`
and `_tl_location` already defined.

The Record TC of a clip is `PyClip.start_time`; the segment's `record_in` follows it (flame-tc-sync
writes `start_time` and reads `record_in` back). The Source TC is the segment's `source_in`.

What real Flame may change is confined to these functions:
  - `_rtc_start_frame(clip)` / `_rtc_write_start(clip, frame)`: reading and writing the Record TC
    as a frame number, in the clip's frame rate
  - `_rtc_source_extension(seg)`: the extension of the source file (DPX is not trusted)
  - `_rtc_facts(clip)`: everything the server judges a clip by (`tc_rules.judge`)
"""

from __future__ import annotations

TC_HELPER = """

def _rtc_time_frame(attr):
    value = _v(attr)
    frame = getattr(value, 'frame', None)
    return frame if isinstance(frame, int) and not isinstance(frame, bool) else None


def _rtc_start_frame(clip):
    return _rtc_time_frame(clip.start_time)


def _rtc_current_frame(clip):
    return _rtc_time_frame(getattr(clip, 'current_time', None))


def _rtc_write_start(clip, frame):
    clip.start_time.set_value(flame.PyTime(frame, str(_v(clip.frame_rate))))


def _rtc_write_current(clip, frame):
    clip.current_time.set_value(flame.PyTime(frame, str(_v(clip.frame_rate))))


def _rtc_source_extension(seg):
    import os
    path = _try(lambda: _v(seg.file_path))
    if not isinstance(path, str) or not path:
        return None
    return os.path.splitext(path)[1].lstrip('.').lower() or None


def _rtc_segments(clip):
    versions = list(clip.versions)
    segments = []
    if len(versions) == 1:
        for trk in list(versions[0].tracks):
            segments.extend(list(trk.segments))
    return len(versions), segments


def _rtc_facts(clip):
    facts = {
        'type': type(clip).__name__, 'name': _try(lambda: _v(clip.name)),
        'uid': _try(lambda: _v(clip.uid)), 'wiretapId': _try(lambda: clip.get_wiretap_node_id()),
        'location': None, 'versionCount': None, 'segmentCount': None,
        'firstSegmentType': None, 'startTimeFrame': None, 'currentTimeFrame': None,
        'startFrame': None, 'recordInFrame': None, 'sourceInFrame': None, 'frameRate': None,
        'sourceFrameRate': None, 'sourceExtension': None, 'selector': None,
    }
    facts['selector'] = (
        {'kind': 'wiretap', 'id': facts['wiretapId']} if facts['wiretapId'] else None
    )
    if facts['type'] != 'PyClip':
        return facts
    facts['location'] = _try(lambda: _tl_location(clip))
    facts['frameRate'] = _try(lambda: str(_v(clip.frame_rate)))
    facts['startFrame'] = _try(lambda: _v(clip.start_frame))
    facts['startTimeFrame'] = _try(lambda: _rtc_start_frame(clip))
    facts['currentTimeFrame'] = _try(lambda: _rtc_current_frame(clip))
    count, segments = _rtc_segments(clip)
    facts['versionCount'] = count
    if count != 1:
        return facts
    facts['segmentCount'] = len(segments)
    if segments:
        facts['firstSegmentType'] = _try(lambda: _j(segments[0].type))
    if len(segments) == 1:
        seg = segments[0]
        facts['recordInFrame'] = _try(lambda: seg.record_in.frame)
        facts['sourceInFrame'] = _try(lambda: seg.source_in.frame)
        facts['sourceFrameRate'] = _try(lambda: str(_v(seg.source_frame_rate)))
        facts['sourceExtension'] = _rtc_source_extension(seg)
    return facts


def _rtc_facts_many(clips):
    return {'rows': [_rtc_facts(c) for c in clips]}


_TS_KEYS = ('start_time_frame', 'current_time_frame')


def _rtc_check_clip(clip):
    kind = type(clip).__name__
    if kind != 'PyClip':
        raise _OpError('SELECTOR_TYPE', 'the target is a ' + kind + ', not a PyClip')


def _rtc_read_keys(clip, keys):
    readers = {'start_time_frame': _rtc_start_frame, 'current_time_frame': _rtc_current_frame}
    attrs = {}
    for key in keys:
        if key not in readers:
            attrs[key] = {'error': 'unknown key ' + repr(key)}
            continue
        try:
            value = readers[key](clip)
        except BaseException as exc:
            attrs[key] = {'error': type(exc).__name__ + ': ' + str(exc)}
            continue
        attrs[key] = {'value': value} if value is not None else {'error': 'not readable'}
    return attrs


def _rtc_get(clip, keys):
    _rtc_check_clip(clip)
    return {
        'type': 'PyClip', 'name': _try(lambda: _v(clip.name)),
        'attributes': _rtc_read_keys(clip, keys), 'stable': _stable_desktop(clip),
    }


def _rtc_set(clip, values, expected):
    _rtc_check_clip(clip)
    _tl_check_not_library(clip, 'the target')
    keys = [k for k in _TS_KEYS if k in values]
    before = {k: v['value'] for k, v in _rtc_read_keys(clip, keys).items() if 'value' in v}
    if expected is not None:
        conflicts = {
            k: {'expected': expected[k], 'current': before.get(k)}
            for k in expected if before.get(k) != expected[k]
        }
        if conflicts:
            raise _OpError('CONFLICT', 'nothing written, the Record TC changed since it was read: '
                           + repr(conflicts))
    writers = {'start_time_frame': _rtc_write_start, 'current_time_frame': _rtc_write_current}
    applied, failed = [], {}
    for key in keys:
        try:
            writers[key](clip, values[key])
            applied.append(key)
        except BaseException as exc:
            failed[key] = type(exc).__name__ + ': ' + str(exc)
    read_back = {k: v.get('value') for k, v in _rtc_read_keys(clip, keys).items()}
    count, segments = _rtc_segments(clip)
    record_in = _try(lambda: segments[0].record_in.frame) if len(segments) == 1 else None
    out = {
        'type': 'PyClip', 'before': before, 'after': read_back, 'applied': applied,
        'failed': failed, 'readBack': read_back, 'recordInFrame': record_in,
        'wiretapId': _try(lambda: clip.get_wiretap_node_id()),
    }
    for key in applied:
        if read_back.get(key) != values[key]:
            out['failed'][key] = (
                'read back ' + repr(read_back.get(key)) + ', asked for ' + repr(values[key])
            )
    out['warnings'] = []
    if 'current_time_frame' in out['failed']:
        out['warnings'].append('current_time_frame: ' + out['failed'].pop('current_time_frame'))
    out['applied'] = [k for k in applied if k not in out['failed']]
    if out['failed']:
        raise _OpError('FLAME_API_ERROR', 'Record TC not as asked: ' + repr(out['failed']))
    return out
"""
