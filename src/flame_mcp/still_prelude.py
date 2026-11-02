"""Prelude source for `render.still` (`ops/render_still.py`, `still_run.py`, `still_check.py`).

Real Python source that `execution.build_exec_code` places ahead of an op's own code, so the op's
`to_python` output is a few loop-free lines. It runs inside Flame (3.11) with `flame`, `_v`, `_ja`,
`_try`, `_OpError` and the helpers of `export_prelude` (`_ex_identity`, `_ex_dir_is_empty`) already
defined.

What it encodes:
  - the frame is written from a reference copy of the clip: `flame.media_panel.copy` into the reel
    `p['reel']` of the first reel group of the Desktop (made when it is not there), the one new
    clip of the reel is the copy, it is renamed to `p['tempName']` and gets the in / out mark
    `p['marks']` (the frame and the one after it: the out mark is not included), then
    `PyExporter.export` runs in the Foreground with `export_between_marks`.
  - the copy is removed at the end -- only while its uid and its name are still the ones it had
    when it was made -- and so is the reel, when this call made it and it is empty.
  - the clip is never touched: no mark of it is set.
  - what the export wrote is not read in Flame: the server lists the output directory.
  - `_st_cleanup` is what `journal.check` runs for a call that never answered: it deletes the
    copies named `p['tempName']` that look like the one that call made, and the reel when it was
    made by that call and is empty.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

STILL_HELPER = """
def _st_groups():
    return list(flame.projects.current_project.current_workspace.desktop.reel_groups)


def _st_find_reel(group, name):
    for reel in list(group.reels):
        if _try(lambda r=reel: _v(r.name)) == name:
            return reel
    return None


def _st_uids(reel):
    return [_try(lambda c=c: _v(c.uid)) for c in list(reel.clips)]


def _st_reel_empty(reel):
    # empty only when clips, sequences and children are all listed and all empty
    for attr in ('clips', 'sequences', 'children'):
        try:
            if len(list(getattr(reel, attr))) != 0:
                return False
        except BaseException:
            return False
    return True


def _st_rows(clips):
    return [
        {
            'uid': _try(lambda c=c: _v(c.uid)),
            'name': _try(lambda c=c: _v(c.name)),
            'wiretapId': _try(lambda c=c: c.get_wiretap_node_id()),
        }
        for c in clips[:5]
    ]


def _st_set_mark(clip, key, value):
    current = getattr(clip, key)
    if type(current).__name__ == 'PyAttribute':
        current.set_value(value)
    else:
        setattr(clip, key, value)


def _st_inspect(clip, reel_name):
    out = _ex_identity(clip)
    out.update(
        duration=None, startFrame=None, version=None, groupFound=False, reelExists=None,
        error=None, errorCode=None,
    )
    if out['type'] != 'PyClip':
        return out
    out['duration'] = _try(lambda: _v(clip.duration).frame)
    out['startFrame'] = _try(lambda: _ja(clip.start_frame))
    out['version'] = _try(lambda: flame.get_version())
    groups = _try(_st_groups) or []
    if groups:
        out['groupFound'] = True
        out['reelExists'] = _st_find_reel(groups[0], reel_name) is not None
    return out


def _st_remove(out, reel, copy, p):
    # the copy goes only while its uid is the one recorded when it was made and its name is the
    # one it was last seen with (the temporary name, or the clip's own when the rename failed)
    if copy is not None:
        same = (
            _try(lambda: _v(copy.uid)) == out['copyUid']
            and _try(lambda: _v(copy.name)) == out['copyName']
        )
        if not same:
            out['tempRemoved'] = False
            out['notes'].append(
                'the temporary copy was renamed or replaced meanwhile, so it was not deleted'
            )
        else:
            try:
                flame.delete(copy, confirm=False)
                out['tempRemoved'] = True
            except BaseException as exc:
                out['tempRemoved'] = False
                out['notes'].append(
                    'deleting the temporary copy raised ' + type(exc).__name__ + ': ' + str(exc)
                )
    if out['tempReelCreated'] and _st_reel_empty(reel):
        try:
            flame.delete(reel, confirm=False)
            out['tempReelRemoved'] = True
        except BaseException as exc:
            out['notes'].append(
                'deleting the temporary reel raised ' + type(exc).__name__ + ': ' + str(exc)
            )


def _st_run(clip, expected_uid, p):
    import time
    out = _ex_identity(clip)
    out.update(
        copyUid=None, copyName=None, copyWiretapId=None, freshCount=None, fresh=[],
        tempRemoved=None, tempReelCreated=False, tempReelRemoved=False,
        returned=None, elapsedS=None, notes=[], error=None, errorCode=None,
    )
    if expected_uid is not None and out['uid'] != expected_uid:
        out['error'] = (
            'the clip found now has uid ' + repr(out['uid']) + ', not the ' + repr(expected_uid)
            + ' that was read before: nothing was copied or exported'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    try:
        empty = _ex_dir_is_empty(p['outputDir'])
    except BaseException as exc:
        out['error'] = (
            'the output directory could not be listed (' + type(exc).__name__ + ': ' + str(exc)
            + '): nothing was copied or exported'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    if not empty:
        out['error'] = (
            'the output directory is not empty any more (something appeared in it after the '
            'server looked): nothing was copied or exported'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    groups = _try(_st_groups) or []
    if not groups:
        out['error'] = 'the Desktop has no reel group to hold the temporary copy'
        out['errorCode'] = 'PRECONDITION_UNMET'
        return out
    reel = _st_find_reel(groups[0], p['reel'])
    if reel is None:
        try:
            reel = groups[0].create_reel(p['reel'])
        except BaseException as exc:
            out['error'] = 'create_reel() raised ' + type(exc).__name__ + ': ' + str(exc)
            out['errorCode'] = 'FLAME_API_ERROR'
            return out
        out['tempReelCreated'] = True
    copy = None
    before = None
    started = time.monotonic()
    try:
        before = _st_uids(reel)
        flame.media_panel.copy(clip, reel, duplicate_action='add')
        fresh = [c for c in list(reel.clips) if _try(lambda c=c: _v(c.uid)) not in before]
        out['freshCount'] = len(fresh)
        if len(fresh) != 1:
            out['fresh'] = _st_rows(fresh)
            raise RuntimeError(
                'the reel gained ' + str(len(fresh)) + ' clips from the copy, not 1: none of '
                'them is taken for the copy and none is deleted'
            )
        copy = fresh[0]
        out['copyUid'] = _try(lambda: _v(copy.uid))
        out['copyName'] = _try(lambda: _v(copy.name))
        out['copyWiretapId'] = _try(lambda: copy.get_wiretap_node_id())
        copy.name.set_value(p['tempName'])
        out['copyName'] = _try(lambda: _v(copy.name))
        _st_set_mark(copy, 'in_mark', p['marks'][0])
        _st_set_mark(copy, 'out_mark', p['marks'][1])
        exporter = flame.PyExporter()
        exporter.foreground = True
        exporter.export_between_marks = True
        out['returned'] = _ja(exporter.export(copy, p['preset'], p['outputDir']))
    except BaseException as exc:
        out['error'] = type(exc).__name__ + ': ' + str(exc)
        out['errorCode'] = 'FLAME_API_ERROR'
        if copy is None and before is not None and out['freshCount'] is None:
            fresh = _try(
                lambda: [c for c in list(reel.clips) if _try(lambda c=c: _v(c.uid)) not in before]
            )
            if fresh:
                out['freshCount'] = len(fresh)
                out['fresh'] = _st_rows(fresh)
    out['elapsedS'] = round(time.monotonic() - started, 3)
    _st_remove(out, reel, copy, p)
    return out


def _st_mismatch(copy, p):
    frames = _try(lambda: _v(copy.duration).frame)
    if frames != p['duration']:
        return 'its duration is ' + repr(frames) + ' frames, not the ' + repr(p['duration'])
    for key, want in (('in_mark', p['marks'][0]), ('out_mark', p['marks'][1])):
        raw = _try(lambda k=key: _v(getattr(copy, k)))
        if raw is None:
            continue
        got = _try(lambda r=raw: r.frame if type(r).__name__ == 'PyTime' else int(r))
        if got != want:
            return 'its ' + key + ' is ' + repr(got) + ', not the ' + repr(want)
    return None


def _st_cleanup(p):
    out = {'found': 0, 'removed': [], 'conflicts': [], 'reelFound': False, 'reelRemoved': False}
    groups = _try(_st_groups) or []
    reel = _st_find_reel(groups[0], p['reel']) if groups else None
    if reel is None:
        return out
    out['reelFound'] = True
    for clip in list(reel.clips):
        if _try(lambda c=clip: _v(c.name)) != p['tempName']:
            continue
        out['found'] += 1
        row = {'uid': _try(lambda c=clip: _v(c.uid)), 'name': p['tempName']}
        why = _st_mismatch(clip, p)
        if why is not None:
            row['why'] = why
            out['conflicts'].append(row)
            continue
        try:
            flame.delete(clip, confirm=False)
            out['removed'].append(row)
        except BaseException as exc:
            row['why'] = 'delete raised ' + type(exc).__name__ + ': ' + str(exc)
            out['conflicts'].append(row)
    if (
        not p['reelPreexisting']
        and out['removed']
        and not out['conflicts']
        and _st_reel_empty(reel)
    ):
        try:
            flame.delete(reel, confirm=False)
            out['reelRemoved'] = True
        except BaseException as exc:
            out['conflicts'].append(
                {'uid': None, 'name': p['reel'], 'why': 'delete raised ' + str(exc)}
            )
    return out
"""
