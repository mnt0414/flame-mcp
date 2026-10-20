"""Prelude source for the segment edits: `segment.slip`, `segment.replace`, the five
effect ops (`segment.create_effect` / `delete_effect` / `set_effect` / `effect_load_setup` /
`effect_save_setup`); `ops/segment_edit.py`, `ops/segment_effects.py`.

Like `timeline_trim_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code, so each op's `to_python` output is a few loop-free lines. It runs inside Flame (3.11)
with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`, `_attr_preflight` / `_set_attributes` /
`_attr_equal`, `_dir_files`, the `_tl_*` helpers of `timeline_prelude` / `timeline_trim_prelude` and
the `_sc_*` helpers of `segment_connection_prelude` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `seg.slip(n)` moves source_in (and source_out) by -n: n > 0 takes the source toward its head.
    record is unchanged, head goes by -n and tail by +n. The return value False is not a failure,
    and an amount beyond the handle does not raise: it stops at the handle (+ at head, - at tail).
  - `seg.smart_replace_media(clip)` (TimelineFX kept) / `seg.smart_replace(clip)` (TimelineFX
    replaced too) answer None. They keep the same offset from the start of the clip, not the source
    timecode; record and head are unchanged, tail follows the new media's length. A clip too short
    for the range changes nothing and raises nothing. source_name becomes the new clip's name. The
    segments connected to the segment (`connected_segments()`) get the new media too, in other
    sequences, and a restore of this sequence does not put those back: the result says how many.
  - `seg.create_effect(type)` answers a PyTimelineFX; a type that is not in `seg.effect_types`
    raises RuntimeError('Could not create an effect of given effect type. ...'), a second effect
    of one type RuntimeError('An effect of this type already exists.'). `seg.effects` is not in the
    order the effects were created.
  - `flame.delete(fx, confirm=False)` answers True and the effect leaves `seg.effects`.
  - a sequence whose bit depth differs from a segment's `source_bit_depth` gets a Resize on that
    segment from Flame itself (a sequence of the same depth gets none; 2025.2.4 and 2026.2.3).
    One put on by hand on a same-depth sequence is deleted with `flame.delete`; the
    automatic one raised "The Timeline FX cannot be deleted." on 2026.2.3 (after the checkpoint
    and the journal intent; not measured on 2025.2.4), so `segment.delete_effect` refuses a
    Resize in that situation first. Not judged when either depth cannot be read.
  - most effect types list only `bypass` as an attribute; Burn-in Letterbox also `mode` (an
    enumeration, `PyAttribute.values` is the tuple of candidates), `ratio` (values = (min, max))
    and `colour` (values = ((r_min, g_min, b_min), (r_max, g_max, b_max))). `set_value` writes
    them. A value outside `values` is refused here first.
  - `fx.save_setup(path)` answers True and writes files whose extension depends on the type
    (`.blur_node` ..., two files for Burn-in Letterbox), so the files are found by comparing the
    directory before and after. `fx.load_setup(path)` answers True; a path that is not there
    raises RuntimeError('Could not load the setup. Check the filepath.') and changes nothing; so
    does a setup of another type. A damaged file is not special-cased: whatever happens is reported.
  - a segment inside a library cannot be edited (Clip is locked.): the edits refuse it first; the
    two reads (`effect_save_setup`) do not mind.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

EDIT_HELPER = """
_TE_MODES = ('media', 'media_and_fx')
_TE_SIMILAR = 10


def _te_start(seg, parent, label, edit):
    # the checks every segment edit starts with, answered as {before, error, errorCode}: the
    # sequence, where it is (an edit needs the Desktop; a read does not mind a library) and a Gap
    out = {'ran': False, 'before': None, 'error': None, 'errorCode': None}
    _tl_check_media(parent, 'the sequence')
    if edit:
        refusal = _tl_refuse_location(parent, 'the sequence')
        if refusal is not None:
            out['error'] = refusal
            out['errorCode'] = 'PRECONDITION_UNMET'
            return out
    out['before'] = _tl_seg_facts(seg)
    if out['before']['type'] == 'Gap':
        out['error'] = 'the segment is a Gap: there is no media to ' + label
        out['errorCode'] = 'PRECONDITION_UNMET'
    return out


def _te_fail(out, code, message):
    out['error'] = message
    out['errorCode'] = code
    return out


def _te_attr_row(fx):
    attrs = {}
    for key in _try(lambda: list(fx.attributes)) or []:
        try:
            attrs[key] = _ja(getattr(fx, key))
        except BaseException as exc:
            attrs[key] = {'error': str(exc) or type(exc).__name__}
    return attrs


def _te_fx_rows(seg):
    rows = []
    for i, fx in enumerate(list(seg.effects)):
        rows.append({'index': i, 'type': _j(fx.type), 'attributes': _te_attr_row(fx)})
    return rows


def _te_fx_types(seg):
    return [_j(fx.type) for fx in list(seg.effects)]


def _te_fx_find(seg, fx_type):
    # the effect of this type on the segment (an exact spelling wins, else ignoring case), or None
    found = None
    for fx in list(seg.effects):
        if _j(fx.type) == fx_type:
            return fx
        if found is None and str(_j(fx.type)).lower() == str(fx_type).lower():
            found = fx
    return found


def _te_fx_missing(out, seg, fx_type, what):
    have = _te_fx_types(seg)
    out['effectTypes'] = have
    return _te_fail(
        out,
        'PRECONDITION_UNMET',
        'the segment has no effect of type ' + repr(fx_type) + ' (it has ' + repr(have) + ')'
        + what,
    )


def _te_position(parent, version, track, record_in):
    index, found = _tl_find_at(parent, version, track, record_in)
    if found is None:
        return None
    return _tl_position(parent, version, track, index, found)


def _te_canon_type(types, wanted):
    # (the spelling `types` lists, every match ignoring case): an exact spelling wins; otherwise
    # the one type that matches ignoring case
    if wanted in types:
        return wanted, [wanted]
    low = str(wanted).lower()
    hits = [t for t in types if str(t).lower() == low]
    return (hits[0] if len(hits) == 1 else None), hits


def _te_slip_expected(before, offset):
    return {
        'recordIn': 0,
        'recordOut': 0,
        'sourceIn': -offset,
        'sourceOut': -offset,
        'head': -offset if isinstance(before['head'], int) else 0,
        'tail': offset if isinstance(before['tail'], int) else 0,
    }


def _te_slip_check(seg, parent, offset):
    out = _te_start(seg, parent, 'slip', True)
    if out['error'] is not None:
        return out
    _tl_judge_source(seg, out)
    if out['error'] is not None:
        return out
    before = out['before']
    edge = 'head' if offset > 0 else 'tail'
    handle = before[edge]
    out['handle'] = {'head': before['head'], 'tail': before['tail']}
    out['available'] = handle
    if handle != 'infinite' and (not isinstance(handle, int) or abs(offset) > handle):
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'cannot slip by ' + str(offset) + ': the ' + edge + ' handle is ' + repr(handle)
            + ' (a ' + ('positive' if offset > 0 else 'negative') + ' offset takes the source '
            + ('toward its head' if offset > 0 else 'toward its tail') + ', and Flame would stop '
            'at the handle without a word)',
        )
    return out


def _te_slip(seg, parent, version, track, offset):
    out = _te_slip_check(seg, parent, offset)
    if out['error'] is not None:
        return out
    numbers = _tl_seg_numbers(seg)
    before_rows = _tl_track_rows(parent, version, track)
    expected = _te_slip_expected(out['before'], offset)
    out.update(
        after=None,
        position=None,
        track=None,
        returned=None,
        deltas=None,
        expectedDeltas=_tl_expected_shown(expected, out['sourceJudged']),
        requested=offset,
        moved=None,
        ran=True,
    )
    out['returned'] = _ja(seg.slip(offset))
    after_rows = _tl_track_rows(parent, version, track)
    out['track'] = _tl_track_summary(parent, version, track)
    if after_rows == before_rows:
        return _te_fail(
            out,
            'SILENT_NOOP',
            'slip(' + str(offset) + ') returned ' + repr(out['returned'])
            + ' and nothing in the track changed',
        )
    return _te_slip_judge(out, parent, version, track, numbers, expected, after_rows)


def _te_slip_judge(out, parent, version, track, numbers, expected, after_rows):
    # reads the slipped segment back (the record must not have moved) and judges it
    offset = out['requested']
    index, found = _tl_find_at(parent, version, track, numbers['recordIn'])
    if found is None:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'the track changed but no segment starts at record_in ' + str(numbers['recordIn'])
            + ' any more (slip must not move the record): the track is now '
            + repr(after_rows[:40]),
        )
    out['after'] = _tl_seg_facts(found)
    out['deltas'] = _tl_deltas(numbers, _tl_seg_numbers(found))
    out['moved'] = numbers['sourceIn'] - found.source_in.frame
    out['position'] = _tl_position(parent, version, track, index, found)
    if not _tl_deltas_match(out['deltas'], expected, out['sourceJudged']):
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'the segment changed, but not by the ' + str(offset) + ' frame(s) asked for: the '
            'source moved by ' + str(out['moved']) + ' (expected ' + repr(out['expectedDeltas'])
            + ', read back ' + repr(out['deltas'])
            + (', source side not judged: ' + out['sourceNotJudgedBecause']
               if not out['sourceJudged'] else '') + ')',
        )
    return out


def _te_replace_check(seg, parent, source, mode):
    out = _te_start(seg, parent, 'replace', True)
    if out['error'] is not None:
        return out
    if mode not in _TE_MODES:
        return _te_fail(
            out,
            'INVALID_ARGS',
            'mode must be one of ' + repr(list(_TE_MODES)) + ', got ' + repr(mode),
        )
    kind = type(source).__name__
    if kind != 'PyClip':
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'source is a ' + kind + ', not a PyClip: smart_replace takes a clip',
        )
    out['source'] = _dc_with_selector(source)
    return out


_TE_REPLACE_KEYS = ('sourceName', 'sourceIn', 'sourceOut', 'head', 'tail')


def _te_replace_key(facts, key):
    value = facts[key]
    return value['frame'] if isinstance(value, dict) else value


def _te_replace(seg, parent, version, track, source, mode):
    out = _te_replace_check(seg, parent, source, mode)
    if out['error'] is not None:
        return out
    numbers = _tl_seg_numbers(seg)
    connected, problem = _sc_others(seg, parent, version, track, {})
    out.update(
        mode=mode,
        after=None,
        position=None,
        returned=None,
        effects={'before': _te_fx_types(seg), 'after': None},
        connected={'count': connected, 'error': problem},
        notices=[_sc_replace_notice(connected)] if connected else [],
        warnings=[],
        ran=True,
    )
    before = out['before']
    if mode == 'media':
        returned = seg.smart_replace_media(source)
    else:
        returned = seg.smart_replace(source)
    out['returned'] = _ja(returned)
    index, found = _tl_find_at(parent, version, track, numbers['recordIn'])
    if found is None:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'no segment starts at record_in ' + str(numbers['recordIn']) + ' after the replace '
            '(it keeps its record): the track is now '
            + repr(_tl_track_rows(parent, version, track)[:40]),
        )
    after = _tl_seg_facts(found)
    out['after'] = after
    out['effects']['after'] = _te_fx_types(found)
    out['position'] = _tl_position(parent, version, track, index, found)
    moved = [k for k in _TE_REPLACE_KEYS if _te_replace_key(before, k) != _te_replace_key(after, k)]
    out['changed'] = moved
    if not moved:
        return _te_fail(
            out,
            'SILENT_NOOP',
            'smart_replace' + ('_media' if mode == 'media' else '') + ' returned '
            + repr(out['returned']) + ' and the segment is the same (source '
            + repr(before['sourceName']) + ', source range '
            + str(before['sourceIn']['frame']) + '-' + str(before['sourceOut']['frame'])
            + ', head ' + repr(before['head']) + ', tail ' + repr(before['tail'])
            + '). Flame does this when the new clip is too short for the '
            'range the segment uses (it keeps the offset from the start of the clip) -- and when '
            'the clip is the one the segment already has',
        )
    for key in ('recordIn', 'recordOut'):
        if before[key]['frame'] != after[key]['frame']:
            out['warnings'].append(
                key + ' changed from ' + str(before[key]['frame']) + ' to '
                + str(after[key]['frame']) + ' (a replace is measured to keep the record)'
            )
    return out


def _te_create_check(seg, parent, fx_type):
    out = _te_start(seg, parent, 'put an effect on', True)
    if out['error'] is not None:
        return out
    types = [_j(t) for t in list(seg.effect_types)]
    canonical, hits = _te_canon_type(types, fx_type)
    out['requestedType'] = fx_type
    if canonical is None:
        low = str(fx_type).lower()
        out['availableTypes'] = types
        out['similar'] = [t for t in types if low in str(t).lower() or str(t).lower() in low][
            :_TE_SIMILAR
        ]
        if len(hits) > 1:
            return _te_fail(
                out,
                'INVALID_ARGS',
                repr(fx_type) + ' matches ' + str(len(hits)) + ' effect types ignoring case '
                + repr(hits) + ': give the exact spelling',
            )
        return _te_fail(
            out,
            'INVALID_ARGS',
            repr(fx_type) + ' is not an effect type this segment accepts (case does not matter; '
            'availableTypes lists them)',
        )
    out['type'] = canonical
    if _te_fx_find(seg, canonical) is not None:
        out['effectTypes'] = _te_fx_types(seg)
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'the segment already has an effect of type ' + repr(canonical) + ' (a segment holds '
            'one of each type: Flame answers "An effect of this type already exists.")',
        )
    return out


def _te_create_effect(seg, parent, version, track, fx_type):
    out = _te_create_check(seg, parent, fx_type)
    if out['error'] is not None:
        return out
    record_in = out['before']['recordIn']['frame']
    out.update(
        ran=True,
        effects={'before': _te_fx_rows(seg), 'after': None},
        created=None,
        position=None,
        returned=None,
    )
    canonical = out['type']
    try:
        made = seg.create_effect(canonical)
    except BaseException as exc:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'create_effect(' + repr(canonical) + ') raised ' + type(exc).__name__ + ': ' + str(exc),
        )
    out['returned'] = {'type': type(made).__name__, 'effectType': _try(lambda: _j(made.type))}
    out['effects']['after'] = _te_fx_rows(seg)
    out['position'] = _te_position(parent, version, track, record_in)
    now = [r for r in out['effects']['after'] if r['type'] == canonical]
    if len(now) != 1 or len(out['effects']['after']) != len(out['effects']['before']) + 1:
        return _te_fail(
            out,
            'SILENT_NOOP',
            'create_effect(' + repr(canonical) + ') answered a ' + type(made).__name__
            + ' but the segment now has ' + repr([r['type'] for r in out['effects']['after']])
            + ' (it had ' + repr([r['type'] for r in out['effects']['before']]) + ')',
        )
    out['created'] = now[0]
    return out


def _te_auto_resize_check(out, seg, parent):
    # a Resize on a segment whose source bit depth differs from the sequence's is the one Flame
    # adds by itself; it is judged only when both depths are read as integers
    sequence_depth = _try(lambda: _ja(parent.bit_depth))
    source_depth = _try(lambda: _ja(seg.source_bit_depth))
    out['sequenceBitDepth'] = sequence_depth
    out['sourceBitDepth'] = source_depth
    if not isinstance(sequence_depth, int) or not isinstance(source_depth, int):
        return out
    if sequence_depth == source_depth:
        return out
    out['refusalHint'] = (
        'A Resize that is treated as the one Flame adds because the sequence and the media '
        'differ in bit depth is not deleted here. Make a sequence with the media bit depth '
        '(timeline.create_sequence with bit_depth=' + str(source_depth) + ' for this media) '
        'and put the media there: Flame adds no Resize when the depths are the same. Nothing '
        'was copied or journaled.'
    )
    return _te_fail(
        out,
        'PRECONDITION_UNMET',
        'this Resize is treated as the one Flame adds by itself: the sequence is an '
        + str(sequence_depth) + '-bit sequence and the media of the segment is '
        + str(source_depth) + '-bit, and Flame puts a Resize on a segment whose source bit '
        'depth differs from that of the sequence, so it is not deleted here (on Flame 2026.2.3 '
        'Flame refused to delete such a Resize with "The Timeline FX cannot be deleted." and '
        'left a changed sequence behind)',
    )


def _te_delete_check(seg, parent, fx_type):
    out = _te_start(seg, parent, 'take an effect off', True)
    if out['error'] is not None:
        return out
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, '')
    out['type'] = _j(fx.type)
    if out['type'] == 'Resize':
        return _te_auto_resize_check(out, seg, parent)
    return out


def _te_delete_effect(seg, parent, version, track, fx_type):
    out = _te_delete_check(seg, parent, fx_type)
    if out['error'] is not None:
        return out
    record_in = out['before']['recordIn']['frame']
    fx = _te_fx_find(seg, fx_type)
    out.update(
        ran=True, effects={'before': _te_fx_rows(seg), 'after': None}, position=None, returned=None
    )
    out['returned'] = _ja(flame.delete(fx, confirm=False))
    out['effects']['after'] = _te_fx_rows(seg)
    out['position'] = _te_position(parent, version, track, record_in)
    still = [r for r in out['effects']['after'] if r['type'] == out['type']]
    if still:
        return _te_fail(
            out,
            'SILENT_NOOP',
            'flame.delete returned ' + repr(out['returned']) + ' and the segment still has an '
            'effect of type ' + repr(out['type']),
        )
    if out['returned'] is not True:
        out['warnings'] = [
            'flame.delete returned ' + repr(out['returned']) + ' although the effect is gone'
        ]
    return out


def _te_tuple_problem(fx, key, want):
    # a tuple attribute whose `values` is (lows, highs): every element inside its own range
    try:
        allowed = _attr_values(getattr(fx, key))
        current = _v(getattr(fx, key))
    except BaseException:
        return None
    if not isinstance(current, tuple) or not allowed or len(allowed) != 2:
        return None
    lows, highs = allowed
    if not isinstance(lows, (list, tuple)) or not isinstance(highs, (list, tuple)):
        return None
    if not isinstance(want, tuple) or not (len(want) == len(lows) == len(highs)):
        return 'rejected: expected ' + str(len(lows)) + ' numbers, got ' + repr(want)
    for i, item in enumerate(want):
        if not _is_num(item):
            return 'rejected: element ' + str(i) + ' is ' + repr(item) + ', not a number'
        if item < lows[i] or item > highs[i]:
            return (
                'rejected: element ' + str(i) + ' (' + repr(item) + ') is outside its range '
                + repr((lows[i], highs[i]))
            )
    return None


def _te_set_problems(fx, values):
    names = list(fx.attributes)
    problems = {}
    for key in values:
        reason, want, current = _attr_preflight(fx, names, key, values[key])
        if reason is None:
            reason = _te_tuple_problem(fx, key, want)
        if reason is not None:
            problems[key] = reason
    return problems


def _te_set_check(seg, parent, fx_type, values):
    out = _te_start(seg, parent, 'set an effect on', True)
    if out['error'] is not None:
        return out
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, ' (segment.create_effect makes one)')
    out['type'] = _j(fx.type)
    out['attributes'] = list(fx.attributes)
    if not isinstance(values, dict) or len(values) == 0:
        return _te_fail(out, 'INVALID_ARGS', 'values must be a non-empty object {attribute: value}')
    problems = _te_set_problems(fx, values)
    if problems:
        out['problems'] = problems
        return _te_fail(
            out,
            'INVALID_ARGS',
            'nothing was written: ' + '; '.join(k + ': ' + problems[k] for k in problems)
            + ' (this effect lists ' + repr(out['attributes']) + ')',
        )
    return out


def _te_set_effect(seg, parent, version, track, fx_type, values):
    out = _te_set_check(seg, parent, fx_type, values)
    if out['error'] is not None:
        return out
    record_in = out['before']['recordIn']['frame']
    fx = _te_fx_find(seg, fx_type)
    out.update(ran=True, applied=[], failed={}, unchanged=[], effect=None, position=None)
    done = _set_attributes(fx, values)
    out['before'] = done['before']
    out['after'] = done['after']
    out['applied'] = done['applied']
    out['failed'] = done['failed']
    out['unchanged'] = [
        k for k in done['applied'] if _attr_equal(done['before'][k], done['after'][k])
    ]
    out['effect'] = {'type': out['type'], 'attributes': _te_attr_row(fx)}
    out['position'] = _te_position(parent, version, track, record_in)
    if done['failed']:
        reasons = list(done['failed'].values())
        if any(r.startswith('silent_noop') for r in reasons):
            code = 'SILENT_NOOP'
        elif any(r.startswith('rejected') for r in reasons):
            code = 'INVALID_ARGS'
        else:
            code = 'FLAME_API_ERROR'
        return _te_fail(
            out,
            code,
            'applied ' + str(len(done['applied'])) + ', failed ' + str(len(done['failed'])) + ' ('
            + '; '.join(k + ': ' + done['failed'][k] for k in done['failed']) + ')',
        )
    return out


def _te_setup_file_problem(path):
    import os
    if not isinstance(path, str) or not os.path.isabs(path):
        return 'INVALID_ARGS', 'path must be an absolute path, got ' + repr(path)
    if not os.path.isfile(path):
        return 'PRECONDITION_UNMET', (
            'no setup file at ' + repr(path) + ': load_setup was NOT called (Flame raises '
            '"Could not load the setup. Check the filepath." for it)'
        )
    if os.path.getsize(path) == 0:
        return 'PRECONDITION_UNMET', 'the setup file ' + repr(path) + ' is empty (0 bytes)'
    return None, None


def _te_load_check(seg, parent, fx_type, path):
    out = _te_start(seg, parent, 'load a setup into', True)
    if out['error'] is not None:
        return out
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, ' (segment.create_effect first)')
    out['type'] = _j(fx.type)
    code, message = _te_setup_file_problem(path)
    if message is not None:
        return _te_fail(out, code, message)
    return out


def _te_changed_keys(before, after):
    keys = sorted(set(before) | set(after))
    return [k for k in keys if not _attr_equal(before.get(k), after.get(k))]


def _te_load_setup(seg, parent, version, track, fx_type, path):
    import os
    out = _te_load_check(seg, parent, fx_type, path)
    if out['error'] is not None:
        return out
    record_in = out['before']['recordIn']['frame']
    fx = _te_fx_find(seg, fx_type)
    rows_before = _tl_track_rows(parent, version, track)
    attrs_before = _te_attr_row(fx)
    out.update(
        ran=True,
        path=path,
        size=os.path.getsize(path),
        returned=None,
        exception=None,
        attributes={'before': attrs_before, 'after': None},
        changedKeys=None,
        trackChanged=None,
        position=None,
    )
    try:
        out['returned'] = _ja(fx.load_setup(path))
    except BaseException as exc:
        out['exception'] = type(exc).__name__ + ': ' + str(exc)
    fx_after = _te_fx_find(seg, fx_type)
    attrs_after = _te_attr_row(fx_after) if fx_after is not None else None
    out['attributes']['after'] = attrs_after
    out['changedKeys'] = _te_changed_keys(attrs_before, attrs_after or {})
    out['trackChanged'] = _tl_track_rows(parent, version, track) != rows_before
    out['position'] = _te_position(parent, version, track, record_in)
    out['effects'] = _te_fx_types(seg)
    if out['exception'] is not None:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'load_setup raised ' + out['exception'] + ' (the effect list and the segment are '
            + ('unchanged' if not out['trackChanged'] else 'CHANGED: see trackChanged') + ')',
        )
    if out['returned'] is not True:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'load_setup returned ' + repr(out['returned']) + ' (True expected)',
        )
    return out


def _te_save_check(seg, parent, fx_type, path, overwrite):
    import os
    out = _te_start(seg, parent, 'save an effect of', False)
    if out['error'] is not None:
        return out
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, '')
    out['type'] = _j(fx.type)
    if not isinstance(path, str) or not os.path.isabs(path):
        return _te_fail(out, 'INVALID_ARGS', 'path must be an absolute path, got ' + repr(path))
    directory, name = os.path.split(path)
    if not name:
        return _te_fail(out, 'INVALID_ARGS', 'path names a directory, not a file: ' + repr(path))
    if not os.path.isdir(directory):
        return _te_fail(
            out, 'PRECONDITION_UNMET', 'the directory does not exist: ' + repr(directory)
        )
    existing = sorted(
        p for p in _dir_files(directory) if os.path.basename(p).startswith(name + '.')
    )
    out['existing'] = existing
    if existing and not overwrite:
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'save_setup would write next to ' + repr(existing) + '; pass overwrite=true to allow '
            'that',
        )
    return out


def _te_save_setup(seg, parent, fx_type, path, overwrite):
    import os
    out = _te_save_check(seg, parent, fx_type, path, overwrite)
    if out['error'] is not None:
        return out
    fx = _te_fx_find(seg, fx_type)
    directory, name = os.path.split(path)
    before = _dir_files(directory)
    out.update(ran=True, requestedPath=path, returned=None, files=[], overwritten=[], warnings=[])
    out['returned'] = _ja(fx.save_setup(path))
    after = _dir_files(directory)
    written = sorted(p for p in after if p not in before or after[p] != before[p])
    out['files'] = [{'path': p, 'size': after[p][1]} for p in written]
    out['overwritten'] = [p for p in written if p in before]
    if not written:
        return _te_fail(
            out,
            'SILENT_NOOP',
            'save_setup returned ' + repr(out['returned']) + ' and ' + repr(directory)
            + ' has no new or changed file',
        )
    stray = [p for p in written if not os.path.basename(p).startswith(name)]
    if stray:
        out['warnings'].append(
            'files that do not start with the name asked for appeared: ' + repr(stray)
        )
    if out['returned'] is not True:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'save_setup returned ' + repr(out['returned']) + ' (True expected)',
        )
    return out
"""
