"""Prelude source for `render.clip` (`ops/render_clip.py`, `render_clip_run.py`).

Like `render_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code, so the op's `to_python` output is a few loop-free lines. It runs inside Flame (3.11) with
`flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`, `_tl_location` and `_ex_identity` (from
`export_prelude`) already defined.

What it encodes:
  - `PyClip.render(render_mode='All', render_option='Foreground', render_quality='Full Resolution',
    effect_type='', effect_caching_mode='Current', include_handles=False) -> bool`, and
    `is_rendered(top_only=False, render_quality='Full Resolution') -> bool`, exist on PyClip and
    on PySequence. Measured on Flame 2025.2.4: a value outside the lists raises RuntimeError and
    names the accepted ones; a clip without Timeline FX reads as rendered; a render of a clip
    with Timeline FX makes `is_rendered()` True and changes neither its uid nor its wiretap id;
    `flush_renders()` does not make it False again.
  - `render_option` is always 'Foreground' and `effect_caching_mode` stays at Flame's 'Current':
    neither is a parameter of the op.
  - the render is judged by `is_rendered()` read before and after, with the same `render_quality`
    and `top_only` (True for render_mode 'Top') that the render was asked with. Reading it costs
    about 55 ms a clip, so a call reads it at most three times (the pre-read, then before and after
    the render).

Both functions answer a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

RENDER_CLIP_HELPER = """
_RC_MEDIA_TYPES = ('PyClip', 'PySequence')


def _rc_effect_types(clip):
    found = []
    count = 0
    for version in list(clip.versions):
        for track in list(version.tracks):
            for segment in list(track.segments):
                for fx in list(segment.effects):
                    count += 1
                    name = _j(fx.type)
                    if name not in found:
                        found.append(name)
    return found, count


def _rc_is_rendered(clip, render_mode, render_quality):
    return _ja(clip.is_rendered(top_only=render_mode == 'Top', render_quality=render_quality))


def _rc_failure(out, text, exc):
    out['error'] = text + ' raised ' + type(exc).__name__ + ': ' + str(exc)
    out['errorCode'] = 'FLAME_API_ERROR'


def _rc_inspect(clip, render_mode, render_quality):
    out = _ex_identity(clip)
    out.update(
        location=None, effects=[], effectCount=0, isRendered=None, error=None, errorCode=None
    )
    if out['type'] not in _RC_MEDIA_TYPES:
        return out
    out['location'] = _tl_location(clip)
    out['effects'], out['effectCount'] = _rc_effect_types(clip)
    if out['effectCount']:
        try:
            out['isRendered'] = _rc_is_rendered(clip, render_mode, render_quality)
        except BaseException as exc:
            _rc_failure(out, 'is_rendered()', exc)
    return out


def _rc_render(clip, expected_uid, render_mode, render_quality, include_handles):
    import time
    out = _ex_identity(clip)
    out.update(
        returned=None,
        elapsedS=None,
        isRenderedBefore=None,
        isRenderedAfter=None,
        uidAfter=None,
        error=None,
        errorCode=None,
    )
    if expected_uid is not None and out['uid'] != expected_uid:
        out['error'] = (
            'the clip found now has uid ' + repr(out['uid']) + ', not the ' + repr(expected_uid)
            + ' that was read before: nothing was rendered'
        )
        out['errorCode'] = 'CONFLICT'
        return out
    try:
        out['isRenderedBefore'] = _rc_is_rendered(clip, render_mode, render_quality)
    except BaseException as exc:
        _rc_failure(out, 'is_rendered() before the render', exc)
        return out
    started = time.monotonic()
    try:
        out['returned'] = _ja(
            clip.render(
                render_mode=render_mode,
                render_option='Foreground',
                render_quality=render_quality,
                effect_caching_mode='Current',
                include_handles=include_handles,
            )
        )
    except BaseException as exc:
        _rc_failure(out, 'render()', exc)
    out['elapsedS'] = round(time.monotonic() - started, 3)
    try:
        out['isRenderedAfter'] = _rc_is_rendered(clip, render_mode, render_quality)
    except BaseException as exc:
        if out['error'] is None:
            _rc_failure(out, 'is_rendered() after the render', exc)
    out['uidAfter'] = _try(lambda: _v(clip.uid))
    return out
"""
