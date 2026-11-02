"""Prelude source for `timeline.colour_spaces` and the colour check of `render.export`.

Real Python source that `execution.build_exec_code` places ahead of an op's own code. It runs inside
Flame (3.11) with `flame`, `_v`, `_j`, `_try` already defined, and it only reads.

`_cs_scan(seq)` walks every version and every video track of a sequence (a Gap is counted, not
read) and reads one colour space per segment. It never raises for one segment: a segment whose read
raises, or answers something that is not a non-empty string, is listed under `unreadable` and is
never taken for a colour space. The walk stops past `CS_BUDGET_S` seconds (`complete` false).

The judgement of what the Flame side reads (`mixed`, what to warn about) is `colour_check.judge`;
the one thing real Flame may change is `_cs_read`: which attribute or call gives a segment's
colour space.
"""

from __future__ import annotations

from typing import Final

CS_BUDGET_S: Final[float] = 20.0
CS_MAX_FIRST: Final[int] = 5
"""Locations kept for each colour space (and for the unreadable ones); `count` keeps counting."""

COLOUR_HELPER = (
    f"_CS_BUDGET_S = {CS_BUDGET_S!r}\n_CS_MAX_FIRST = {CS_MAX_FIRST}\n"
    + """

def _cs_read(seg):
    # the only place a segment's colour space is read
    return _v(seg.get_colour_space())


def _cs_note(bucket, key, where):
    entry = bucket.setdefault(key, {'count': 0, 'first': []})
    entry['count'] += 1
    if len(entry['first']) < _CS_MAX_FIRST:
        entry['first'].append(where)


def _cs_scan(seq):
    import time
    started = time.monotonic()
    spaces = {}
    unreadable = {'count': 0, 'first': []}
    out = {
        'spaces': spaces, 'unreadable': unreadable, 'segments': 0, 'gaps': 0,
        'versions': [], 'complete': True, 'error': None,
    }
    try:
        for vi, ver in enumerate(list(seq.versions)):
            tracks = list(ver.tracks)
            out['versions'].append({'version': vi, 'tracks': len(tracks)})
            for ti, trk in enumerate(tracks):
                for si, seg in enumerate(list(trk.segments)):
                    if time.monotonic() - started > _CS_BUDGET_S:
                        out['complete'] = False
                        return out
                    if _j(seg.type) == 'Gap':
                        out['gaps'] += 1
                        continue
                    out['segments'] += 1
                    where = {'version': vi, 'track': ti, 'index': si}
                    try:
                        space = _cs_read(seg)
                    except BaseException as exc:
                        where['error'] = type(exc).__name__ + ': ' + str(exc)
                        space = None
                    if isinstance(space, str) and space:
                        _cs_note(spaces, space, where)
                    else:
                        unreadable['count'] += 1
                        if len(unreadable['first']) < _CS_MAX_FIRST:
                            unreadable['first'].append(where)
    except BaseException as exc:
        out['complete'] = False
        out['error'] = type(exc).__name__ + ': ' + str(exc)
    return out
"""
)
