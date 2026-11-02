"""Prelude source for `render.export` (`ops/render_export.py`, `export_run.py`).

Like `render_clip_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code, so the op's `to_python` output is a few loop-free lines. It runs inside Flame (3.11)
with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError` and `_tl_location` already defined.

What it encodes:
  - the export is `PyExporter()` with `foreground = True` and `export_between_marks` set, then
    `export(clip_or_sequence, preset_path, output_directory)`; `export_between_marks` is a property
    (a bool), not a function. With it true the frames from the in mark up to, not including, the
    out mark are written.
  - right before the export the directory is listed again (`_ex_dir_is_empty`: its top level, a
    name starting with `.` left out) and nothing is exported when something has appeared since the
    server looked: CONFLICT. Whatever is in a folder shows as that folder at the top level.
  - what the export wrote is not read in Flame: the server lists the output directory before and
    after (`render_files.scan_tree`). The Flame side only runs the export and answers its return
    value and how long it took.

Both functions answer a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

EXPORT_HELPER = """
_EX_MEDIA_TYPES = ('PyClip', 'PySequence')


def _ex_identity(clip):
    wiretap = _try(lambda: clip.get_wiretap_node_id())
    return {
        'type': type(clip).__name__,
        'name': _try(lambda: _v(clip.name)),
        'uid': _try(lambda: _v(clip.uid)),
        'wiretapId': wiretap,
        'selector': {'kind': 'wiretap', 'id': wiretap} if wiretap else None,
    }


def _ex_inspect(clip):
    out = _ex_identity(clip)
    out.update(duration=None, location=None, colourScan=None, error=None, errorCode=None)
    if out['type'] not in _EX_MEDIA_TYPES:
        return out
    if out['type'] == 'PySequence':
        out['colourScan'] = _cs_scan(clip)
    out['duration'] = _try(lambda: _v(clip.duration).frame)
    out['location'] = _tl_location(clip)
    return out


def _ex_dir_is_empty(output_dir):
    import os
    return not [name for name in os.listdir(output_dir) if not name.startswith('.')]


def _ex_export(clip, expected_uid, preset, output_dir, between_marks):
    import time
    out = _ex_identity(clip)
    out.update(returned=None, elapsedS=None, uidAfter=None, error=None, errorCode=None)
    if expected_uid is not None and out['uid'] != expected_uid:
        out['error'] = (
            'the clip found now has uid ' + repr(out['uid']) + ', not the ' + repr(expected_uid)
            + ' that was read before: nothing was exported'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    try:
        empty = _ex_dir_is_empty(output_dir)
    except BaseException as exc:
        out['error'] = (
            'the output directory could not be listed (' + type(exc).__name__ + ': ' + str(exc)
            + '): nothing was exported'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    if not empty:
        out['error'] = (
            'the output directory is not empty any more (something appeared in it after the '
            'server looked): nothing was exported'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    started = time.monotonic()
    try:
        exporter = flame.PyExporter()
        exporter.foreground = True
        exporter.export_between_marks = between_marks
        out['returned'] = _ja(exporter.export(clip, preset, output_dir))
    except BaseException as exc:
        out['error'] = 'export() raised ' + type(exc).__name__ + ': ' + str(exc)
        out['errorCode'] = 'FLAME_API_ERROR'
    out['elapsedS'] = round(time.monotonic() - started, 3)
    out['uidAfter'] = _try(lambda: _v(clip.uid))
    return out
"""
