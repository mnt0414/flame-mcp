"""Prelude source for `desktop.reformat` (`ops/desktop_reformat.py`, `desktop_reformat_run.py`).

Like `desktop_delete_prelude`: real Python source that `execution.build_exec_code` places ahead of
an op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`
and the `_dc_*` / `_dm_*` / `_tl_*` / `_dd_*` helpers already defined.

What it encodes:
  - `PyClip.reformat(width=0, height=0, ratio=0.0, bit_depth=0, scan_mode='', frame_rate='',
    resize_mode='Letterbox')` answers None and changes the clip in place: no new clip. Its answer
    says nothing, so a reformat is judged by reading the clip's format back.
  - `PyClip.change_start_frame(start_frame, use_segment_connections=True)` answers None.
  - the server sends one clip per Flame call (`_rf_write`).
  - the timeline fingerprint (`_tl_fingerprint`) covers segments and effects only: it does not see
    width, height, bit depth, ratio, scan mode, frame rate or start frame. So the format itself is
    read (`_rf_attrs`) before and after, and both the write's compare-and-swap and the check judge
    by it.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

from flame_mcp.desktop_reformat_state import RATIO_TOLERANCE

_HELPER_TEMPLATE = """
_RF_ATTRS = (
    ('width', 'width'),
    ('height', 'height'),
    ('bitDepth', 'bit_depth'),
    ('ratio', 'ratio'),
    ('scanMode', 'scan_mode'),
    ('frameRate', 'frame_rate'),
    ('startFrame', 'start_frame'),
)
_RF_RATIO_TOLERANCE = __RATIO_TOLERANCE__


def _rf_attrs(obj):
    out = {}
    for key, attr in _RF_ATTRS:
        out[key] = _try(lambda a=attr: _ja(getattr(obj, a)))
    out['duration'] = _try(lambda: obj.duration.frame)
    return out


def _rf_check_target(obj, label):
    kind = type(obj).__name__
    if kind != 'PyClip':
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is a ' + kind + ', not a PyClip: only a clip on the Desktop is reformatted '
            'here (a sequence, a reel, a folder and a library are not)',
        )
    parent = _dm_parent(obj, label)
    where = _tl_location(obj)
    if where == 'batch' or _dc_is_batch_reel(parent):
        raise _OpError(
            'INVALID_ARGS',
            label + ' is inside a reel of a Batch group: a Batch group is not reformatted from '
            'here',
        )
    if where == 'library':
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is inside a library: a clip in a library is not reformatted by this server; '
            'copy it onto the Desktop with desktop.copy first',
        )
    if where != 'desktop' or type(parent).__name__ != 'PyReel':
        raise _OpError(
            'PRECONDITION_UNMET',
            label + ' is not found to be in a reel on the Desktop (its parents are '
            + repr(_tl_chain_types(obj)) + '): nothing is done to what cannot be put back',
        )
    return parent


def _rf_snapshot(targets, spec):
    rows = []
    seen = []
    for i, obj in enumerate(targets):
        label = 'targets[' + str(i) + ']'
        parent = _rf_check_target(obj, label)
        row = _dc_with_selector(obj)
        parent_row = _dc_with_selector(parent)
        if row['uid'] is None or row['selector'] is None or parent_row['selector'] is None:
            raise _OpError(
                'PRECONDITION_UNMET',
                label + ' or its parent has no uid / wiretap id, so it could not be found again '
                'or put back',
            )
        if row['uid'] in seen:
            raise _OpError('INVALID_ARGS', label + ' is listed twice')
        seen.append(row['uid'])
        fp = _tl_fingerprint(obj)
        row['parent'] = parent_row
        row['fingerprint'] = fp['fingerprint']
        row['segmentCount'] = fp['segmentCount']
        row['effectCount'] = fp['effectCount']
        row['attrs'] = _rf_attrs(obj)
        row['siblings'] = [_dm_uid(o) for o in _dm_siblings(parent, obj)]
        need_reformat, need_start = _rf_needs(row['attrs'], spec)
        row['needsWrite'] = need_reformat or need_start
        rows.append(row)
    return {
        'sources': rows,
        'existing': [{'uid': r['uid']} for r in rows],
        'error': None,
        'errorCode': None,
    }


def _rf_needs(before, spec):
    kw = spec['reformat']
    reformat = (
        before['width'] != kw['width']
        or before['height'] != kw['height']
        or before['ratio'] is None
        or abs(before['ratio'] - kw['ratio']) > _RF_RATIO_TOLERANCE
        or ('bit_depth' in kw and before['bitDepth'] != kw['bit_depth'])
        or ('scan_mode' in kw and before['scanMode'] != kw['scan_mode'])
        or ('frame_rate' in kw and before['frameRate'] != kw['frame_rate'])
    )
    start = spec.get('start_frame') is not None and before['startFrame'] != spec['start_frame']
    return reformat, start


def _rf_conflicts(obj, before, expected):
    problems = []
    if _tl_fingerprint(obj)['fingerprint'] != expected.get('fingerprint'):
        problems.append('its segments or effects changed')
    for key, value in (expected.get('attrs') or {}).items():
        if before.get(key) != value:
            problems.append(key + ' is ' + repr(before.get(key)) + ', it was ' + repr(value))
    return problems


def _rf_warnings(kw, after):
    notes = []
    for key, attr in (('bit_depth', 'bitDepth'), ('scan_mode', 'scanMode'),
                      ('frame_rate', 'frameRate')):
        if key in kw and after[attr] != kw[key]:
            notes.append(
                key + ' was asked as ' + repr(kw[key]) + ' and reads back as ' + repr(after[attr])
            )
    if after['ratio'] is None or abs(after['ratio'] - kw['ratio']) > _RF_RATIO_TOLERANCE:
        notes.append(
            'ratio was asked as ' + repr(kw['ratio']) + ' and reads back as ' + repr(after['ratio'])
        )
    return notes


def _rf_judge(out, spec, ran_reformat):
    kw = spec['reformat']
    before = out['before']
    after = out['after']
    size = str(kw['width']) + 'x' + str(kw['height'])
    now = str(after['width']) + 'x' + str(after['height'])
    if out['reformatError'] is not None:
        out['error'] = 'PyClip.reformat raised ' + out['reformatError']
        out['errorCode'] = 'FLAME_API_ERROR'
    elif out['startFrameError'] is not None:
        out['error'] = 'PyClip.change_start_frame raised ' + out['startFrameError']
        out['errorCode'] = 'FLAME_API_ERROR'
    elif ran_reformat and (after['width'] != kw['width'] or after['height'] != kw['height']):
        if after == before:
            out['error'] = (
                'PyClip.reformat answered ' + repr(out['reformatReturned']) + ' without an error '
                'but the clip is still ' + now + ' (asked for ' + size + ')'
            )
            out['errorCode'] = 'SILENT_NOOP'
        else:
            out['error'] = (
                'asked for ' + size + ', the clip reads back as ' + now + ' after PyClip.reformat'
            )
            out['errorCode'] = 'FLAME_API_ERROR'
    elif ran_reformat and after == before:
        out['error'] = (
            'PyClip.reformat answered ' + repr(out['reformatReturned']) + ' without an error but '
            'nothing about the clip changed (asked for ' + repr(kw) + ')'
        )
        out['errorCode'] = 'SILENT_NOOP'
    elif spec.get('start_frame') is not None and after['startFrame'] != spec['start_frame']:
        out['error'] = (
            'start_frame was asked as ' + repr(spec['start_frame']) + ' and PyClip.change_start_'
            'frame answered without an error, but the clip reads back as '
            + repr(after['startFrame'])
        )
        out['errorCode'] = 'FLAME_API_ERROR'
    elif ran_reformat:
        out['warnings'] = _rf_warnings(kw, after)
    out['changed'] = after != before


def _rf_write(obj, spec, expected):
    import time
    out = {
        'before': _rf_attrs(obj),
        'after': None,
        'applied': None,
        'changed': False,
        'skipped': False,
        'reformatReturned': None,
        'reformatError': None,
        'startFrameError': None,
        'reformatSeconds': None,
        'startFrameSeconds': None,
        'fingerprintBefore': _tl_fingerprint(obj)['fingerprint'],
        'fingerprintAfter': None,
        'identAfter': None,
        'state': None,
        'warnings': [],
        'error': None,
        'errorCode': None,
    }
    out['after'] = out['before']
    if expected is not None:
        problems = _rf_conflicts(obj, out['before'], expected)
        if problems:
            out['error'] = (
                'nothing was changed: the clip is not as it was read (' + '; '.join(problems) + ')'
            )
            out['errorCode'] = 'CONFLICT'
            out['fingerprintAfter'] = out['fingerprintBefore']
            return out
    kw = dict(spec['reformat'])
    applied = dict(kw)
    if spec.get('start_frame') is not None:
        applied['start_frame'] = spec['start_frame']
        applied['use_segment_connections'] = spec['use_segment_connections']
    out['applied'] = applied
    need_reformat, _ = _rf_needs(out['before'], spec)
    if need_reformat:
        started = time.monotonic()
        try:
            out['reformatReturned'] = _ja(obj.reformat(**kw))
        except BaseException as exc:
            out['reformatError'] = type(exc).__name__ + ': ' + str(exc)
        out['reformatSeconds'] = round(time.monotonic() - started, 3)
    wanted = spec.get('start_frame')
    after_reformat = _rf_attrs(obj) if need_reformat else out['before']
    ran_start = (
        wanted is not None
        and out['reformatError'] is None
        and after_reformat['startFrame'] != wanted
    )
    if ran_start:
        started = time.monotonic()
        try:
            obj.change_start_frame(spec['start_frame'], spec['use_segment_connections'])
        except BaseException as exc:
            out['startFrameError'] = type(exc).__name__ + ': ' + str(exc)
        out['startFrameSeconds'] = round(time.monotonic() - started, 3)
    out['skipped'] = not need_reformat and not ran_start
    out['after'] = _rf_attrs(obj)
    out['fingerprintAfter'] = _try(lambda: _tl_fingerprint(obj)['fingerprint'])
    out['identAfter'] = _try(lambda: _dc_with_selector(obj))
    out['state'] = _dm_state(obj)
    _rf_judge(out, spec, need_reformat)
    return out


def _rf_stash(targets, library_name, reel_name):
    out = _dd_stash(targets, library_name, reel_name)
    for item in out['stashes']:
        copy = _dc_find_by_uid((item.get('stash') or {}).get('uid'))
        item['sourceAttrs'] = _rf_attrs(targets[item['index']])
        item['stashAttrs'] = _rf_attrs(copy) if copy is not None else None
    return out


def _rf_observe(items):
    rows = {}
    for item in items:
        chain = list(item.get('uids') or [item.get('uid')])
        obj, found = _dm_find_chain(chain, item.get('parent'))
        if obj is None:
            rows[item.get('uid')] = {'found': False}
            continue
        rows[item.get('uid')] = {
            'found': True,
            'foundUid': found,
            'type': type(obj).__name__,
            'ident': _dc_with_selector(obj),
            'attrs': _rf_attrs(obj),
            'fingerprint': _tl_fingerprint(obj)['fingerprint'],
            'state': _dm_state(obj),
        }
    return {'items': rows, 'error': None, 'errorCode': None}
"""

REFORMAT_HELPER = _HELPER_TEMPLATE.replace("__RATIO_TOLERANCE__", repr(RATIO_TOLERANCE))
