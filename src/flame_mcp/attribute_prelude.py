"""Prelude source for the generic attribute ops (`ops/_attributes.py`).

Like `SELECTOR_HELPER`, this is real Python source that `execution.build_exec_code`
places ahead of an op's own code, so the ops' `to_python` output stays a handful
of loop-free lines (registry.py's pacing guard parses only that output). It runs
inside Flame (3.11) with `flame`, `_v`, `_j` and `_OpError` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `PyAttribute.values` is a tuple *attribute*: the candidate list for an
    enumerated attribute, `(min, max)` for a numeric one, `None` for free text.
  - an invalid value raises `RuntimeError('This is not an option')` -- but the
    check here runs *before* `set_value`, so a bad value never reaches Flame.
  - `set_value` is not pushed on Flame's Undo history, and some writes are
    silently ignored (PySegment.dynamic_* stay False after being set to True, with no
    exception): every write is read back
    through a freshly fetched `getattr(obj, key)` and compared.
  - a PyResolution attribute is read as {type, width, height, bit_depth, frame_ratio, scan_mode}
    and written by building a new flame.PyResolution (the fields not given keep their value).
    `set_value` answers True even when the value did not take. One field of a resolution can
    stay unchanged while the others take: bit_depth, which a Colour Source keeps at 8 / 16 / 32
    only (10 and 12 become 16) and a Clip node does not take at all. A write that changed the
    value but not to what was asked (some fields of a resolution; a scalar that Flame rounds)
    ends in `partial:` (FLAME_API_ERROR), one that changed nothing in `silent_noop:`. The keys
    whose value changed are returned as `changedKeys` (applied or not): that is what a revert
    puts back.
  - a PyTime attribute is written for `current_time` only, from {frame} and/or {timecode} (and
    an optional fps that must be the object's own frame_rate). The PyTime is built in the
    absolute form `flame.PyTime(frame, frame_rate)` / `flame.PyTime(timecode, frame_rate)`
    (the relative `PyTime(int)` is never used), and the frame must lie inside
    `start_time.frame` .. `start_time.frame + duration.frame - 1`: Flame moves a frame outside
    that range to the nearest end without an error (58 -> 47, -1 -> 0 on a 48-frame clip), so
    the check runs before the write. Other PyTime attributes (start_time ...) are refused:
    start_time drags current_time along when raised and not when lowered.
  - compare-and-swap: when the server passes `expected` (the values it read a
    moment ago), every key's current value must still equal it, checked after the
    preflight and before the first write; one mismatch means nothing is written and
    the call ends in CONFLICT (this closes the gap between the server's read and the write,
    in which a user could change the value in the UI).
"""

from __future__ import annotations

# Flame matches these labels to its colour coding table ignoring letter case and stores the
# table's spelling (measured on Flame 2025.2.4): the value read back may differ from the one
# written only in case. Shared by the in-Flame read-back check and the journal's comparisons.
CASE_FOLDED_ATTRIBUTES = ("colour_label", "schematic_colour_label")

ATTRIBUTE_HELPER = """
def _wrapper_ref(v):
    # a Flame object (PyTrack, PySegment, ...): its type, and its name when it has a readable one.
    # Never its repr, which carries a memory address that changes on every call.
    out = {'type': type(v).__name__}
    try:
        name = _v(v.name)
    except BaseException:
        name = None
    if isinstance(name, str) and name != '':
        out['name'] = name
    return out


_RES_KEYS = ('width', 'height', 'bit_depth', 'frame_ratio', 'scan_mode')
_RES_BIT_DEPTHS = (8, 10, 12, 16, 32)


def _is_resolution(x):
    return type(x).__name__ == 'PyResolution'


def _resolution_dict(res):
    res = _v(res)
    out = {}
    for key in ('width', 'height', 'bit_depth', 'scan_mode', 'frame_ratio'):
        out[key] = _try(lambda key=key: _ja(getattr(res, key)))
    return out


def _ja(x):
    v = _v(x)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_ja(i) for i in v]
    if type(v).__name__ == 'PyTime':
        return {'timecode': _ja(v.timecode), 'frame': _ja(v.frame)}
    if _is_resolution(v):
        out = {'type': 'PyResolution'}
        out.update(_resolution_dict(v))
        return out
    if type(v).__name__.startswith('Py'):
        return _wrapper_ref(v)
    return repr(v)


def _attr_values(attr):
    try:
        values = attr.values
    except BaseException:
        return None
    if isinstance(values, (list, tuple)):
        return list(values)
    return None


def _get_attributes(obj, keys, include_values):
    names = list(obj.attributes)
    wanted = names if not keys else list(keys)
    out = {}
    for key in wanted:
        try:
            if key not in names:
                raise KeyError('no such attribute')
            attr = getattr(obj, key)
            entry = {'value': _ja(attr)}
            if include_values:
                entry['values'] = _ja(_attr_values(attr))
            out[key] = entry
        except BaseException as exc:
            out[key] = {'error': str(exc) or type(exc).__name__}
    return {'type': type(obj).__name__, 'attributes': out}


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _to_tuple(x):
    if isinstance(x, list):
        return tuple(_to_tuple(i) for i in x)
    return x


def _resolution_gaps(got, want):
    # [(field, asked, read back)] for every field of the resolution `want` that `got` does not hold
    g = _resolution_dict(got)
    w = _resolution_dict(want)
    return [(k, w[k], g[k]) for k in _RES_KEYS if not _attr_equal(g[k], w[k])]


def _attr_equal(a, b):
    if _is_resolution(a) and _is_resolution(b):
        return len(_resolution_gaps(a, b)) == 0
    if type(a).__name__ == 'PyTime' and type(b).__name__ == 'PyTime':
        return _v(a.frame) == _v(b.frame)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return abs(a - b) <= 1e-6
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_attr_equal(x, y) for x, y in zip(a, b))
    return a == b


def _written_equal(key, got, want):
    if key in @CASE_FOLDED@ and isinstance(got, str) and isinstance(want, str):
        return got.lower() == want.lower()
    return _attr_equal(got, want)


def _same_exact(a, b):
    # compare-and-swap equality: both sides are JSON-form values (a tuple attribute is a list)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same_exact(x, y) for x, y in zip(a, b))
    return a == b


def _cas_conflicts(plan, before, expected):
    conflicts = {}
    for key, want, current in plan:
        if key not in expected:
            conflicts[key] = {'expected': None, 'current': before[key], 'note': 'no expected value'}
        elif not _same_exact(before[key], expected[key]):
            conflicts[key] = {'expected': expected[key], 'current': before[key]}
    return conflicts


def _sibling_names(obj, group):
    # Names of the other nodes of `group` (a Batch group): all of them minus one
    # occurrence of obj's own current name. Compared by name, not identity: Flame
    # hands out a fresh wrapper on every access.
    names = [_v(n.name) for n in list(group.nodes)]
    try:
        names.remove(_v(obj.name))
    except ValueError:
        pass
    return names


def _resolution_problem(current, want):
    if not isinstance(want, dict):
        return 'expected an object with any of ' + ', '.join(_RES_KEYS) + ', got ' + repr(want)
    for k in want:
        if k == 'type':
            if want[k] != 'PyResolution':
                return "'type' must be 'PyResolution' when given, got " + repr(want[k])
        elif k not in _RES_KEYS:
            return 'unknown key ' + repr(k) + ' (keys: ' + ', '.join(_RES_KEYS) + ')'
    for k in ('width', 'height'):
        if k in want:
            n = want[k]
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                return k + ' must be a positive integer, got ' + repr(n)
    if 'bit_depth' in want:
        n = want['bit_depth']
        if not isinstance(n, int) or isinstance(n, bool) or n not in _RES_BIT_DEPTHS:
            return 'bit_depth must be one of ' + repr(_RES_BIT_DEPTHS) + ', got ' + repr(n)
    if 'frame_ratio' in want and not _is_num(want['frame_ratio']):
        return 'frame_ratio must be a number, got ' + repr(want['frame_ratio'])
    if 'scan_mode' in want:
        have = _try(lambda: _v(current.scan_mode))
        if want['scan_mode'] != have or not isinstance(have, str):
            return (
                'scan_mode: only the value it has now (' + repr(have) + ') is accepted'
            )
    return None


def _resolution_want(current, want):
    problem = _resolution_problem(current, want)
    if problem is not None:
        return 'rejected: ' + problem, None
    have = _resolution_dict(current)
    merged = {}
    for k in _RES_KEYS:
        merged[k] = want[k] if k in want else have[k]
        if merged[k] is None:
            return 'rejected: the current ' + k + ' cannot be read, so it must be given', None
    try:
        built = flame.PyResolution(
            merged['width'],
            merged['height'],
            merged['bit_depth'],
            merged['frame_ratio'],
            merged['scan_mode'],
        )
    except BaseException as exc:
        return 'rejected: Flame refused to build the resolution: ' + str(exc), None
    return None, built


_TIME_KEYS = ('frame', 'timecode', 'fps')
_TIME_SHAPE = (
    "an object {'frame': int} and/or {'timecode': str}, with an optional 'fps' that must be "
    "the object's own frame rate"
)


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _time_problem(want):
    if not isinstance(want, dict):
        return 'expected ' + _TIME_SHAPE + ', got ' + repr(want)
    for k in want:
        if k not in _TIME_KEYS:
            return 'unknown key ' + repr(k) + ' (keys: ' + ', '.join(_TIME_KEYS) + ')'
    if 'frame' not in want and 'timecode' not in want:
        return 'expected ' + _TIME_SHAPE + ', got ' + repr(want)
    if 'frame' in want and not _is_int(want['frame']):
        return 'frame must be an integer, got ' + repr(want['frame'])
    if 'timecode' in want and not isinstance(want['timecode'], str):
        return 'timecode must be a string, got ' + repr(want['timecode'])
    if 'fps' in want and not isinstance(want['fps'], str):
        return 'fps must be a string, got ' + repr(want['fps'])
    return None


def _time_want(obj, key, want):
    if key != 'current_time':
        reason = (
            'rejected: only current_time can be written among the PyTime attributes ('
            + key + ' is not)'
        )
        if key == 'start_time':
            reason += '; start_time moves current_time along when raised and not when lowered'
        return reason, None
    problem = _time_problem(want)
    if problem is not None:
        return 'rejected: ' + problem, None
    rate = _try(lambda: str(_v(obj.frame_rate)))
    first = _try(lambda: _v(_v(obj.start_time).frame))
    length = _try(lambda: _v(_v(obj.duration).frame))
    if not isinstance(rate, str) or not _is_int(first) or not _is_int(length):
        return 'rejected: the frame rate, start_time or duration of the object cannot be read', None
    if 'fps' in want and want['fps'] != rate:
        return (
            'rejected: fps ' + repr(want['fps']) + ' is not the frame rate of the object ('
            + repr(rate) + '); the time would mean something else'
        ), None
    try:
        if 'frame' in want:
            built = flame.PyTime(want['frame'], rate)
        else:
            built = flame.PyTime(want['timecode'], rate)
        frame = _v(built.frame)
        timecode = _v(built.timecode)
    except BaseException as exc:
        return 'rejected: Flame refused to build the time: ' + str(exc), None
    if 'frame' in want and 'timecode' in want and timecode != want['timecode']:
        return (
            'rejected: frame ' + repr(want['frame']) + ' is timecode ' + repr(timecode)
            + ', not ' + repr(want['timecode'])
        ), None
    if frame < first or frame > first + length - 1:
        return (
            'rejected: frame ' + repr(frame) + ' is outside the range of the object ('
            + repr(first) + ' to ' + repr(first + length - 1)
            + ')'
        ), None
    return None, built


def _attr_preflight(obj, names, key, want):
    if key not in names:
        shown = ', '.join(names[:40]) + (' ...' if len(names) > 40 else '')
        return 'rejected: no such attribute (attributes: ' + shown + ')', None, None
    try:
        attr = getattr(obj, key)
        current = _v(attr)
    except BaseException as exc:
        return 'rejected: cannot read the attribute: ' + str(exc), None, None
    if type(attr).__name__ != 'PyAttribute':
        return 'rejected: not a PyAttribute (' + type(attr).__name__ + ')', None, None
    if type(current).__name__ == 'PyTime':
        reason, built = _time_want(obj, key, want)
        return reason, built, current
    if _is_resolution(current):
        reason, built = _resolution_want(current, want)
        return reason, built, current
    if isinstance(current, tuple):
        want = _to_tuple(want)
    if isinstance(current, bool):
        compatible = isinstance(want, bool)
    elif isinstance(current, int):
        # an integer attribute takes integers only: a float (even 123.0) is refused here,
        # not left to Flame, which may drop it silently
        if isinstance(want, float):
            return (
                'rejected: this attribute holds an integer ('
                + repr(current)
                + '); pass an integer, not the float '
                + repr(want),
                None,
                current,
            )
        compatible = _is_num(want)
    elif _is_num(current):
        compatible = _is_num(want)
    elif isinstance(current, str):
        compatible = isinstance(want, str)
    elif isinstance(current, tuple):
        compatible = isinstance(want, tuple)
    else:
        compatible = True
    if not compatible:
        return (
            'rejected: expected a value shaped like the current '
            + repr(_ja(current))
            + ', got '
            + repr(want),
            None,
            current,
        )
    allowed = _attr_values(attr)
    if allowed and isinstance(current, str):
        if all(isinstance(a, str) for a in allowed) and want not in allowed:
            return 'rejected: ' + repr(want) + ' is not one of ' + repr(allowed), None, current
    if allowed and _is_num(current):
        if len(allowed) == 2 and all(_is_num(a) for a in allowed):
            if want < allowed[0] or want > allowed[1]:
                return (
                    'rejected: ' + repr(want) + ' is outside the range ' + repr(tuple(allowed)),
                    None,
                    current,
                )
    return None, want, current


_NAME_IN_USE_HINT = (
    ' (Flame checks a node name against the CURRENT Batch group, not the '
    "node's own group: the current group may hold a node with that name)"
)

_COLOUR_LABEL_HINT = (
    " (the label must be a name from Flame's colour coding table; letter case does not "
    "matter but spaces do: 'Light Grey' is not 'lightgrey'; '' means no colour)"
)


def _set_attributes(obj, values, group=None, expected=None, restoring=False):
    if not isinstance(values, dict) or len(values) == 0:
        raise _OpError('INVALID_ARGS', 'values must be a non-empty object {attribute: value}')
    names = list(obj.attributes)
    # a revert puts back the name the journal recorded, even when another node has it now
    check_names = group is not None and 'name' in values and not restoring
    others = _sibling_names(obj, group) if check_names else []
    plan = []
    failed = {}
    before = {}
    for key in values:
        reason, want, current = _attr_preflight(obj, names, key, values[key])
        if reason is None and check_names and key == 'name' and want in others:
            reason = (
                'rejected: another node of this Batch group is already named ' + repr(want)
                + ' (a non-current group would silently get two nodes with one name)'
            )
        if reason is not None:
            failed[key] = reason
        else:
            plan.append((key, want, current))
            before[key] = _ja(current)
    after = {}
    applied = []
    revert_values = {}
    conflicts = {}
    if not failed and expected is not None:
        conflicts = _cas_conflicts(plan, before, expected)
        for key in conflicts:
            failed[key] = (
                'conflict: expected '
                + repr(conflicts[key]['expected'])
                + ' but the current value is '
                + repr(conflicts[key]['current'])
            )
    if not failed:
        for key, want, current in plan:
            try:
                getattr(obj, key).set_value(want)
                got = _v(getattr(obj, key))
            except BaseException as exc:
                failed[key] = 'error: ' + type(exc).__name__ + ': ' + str(exc)
                if 'name is already being used' in str(exc):
                    failed[key] += _NAME_IN_USE_HINT
                if 'colour label does not exist' in str(exc):
                    failed[key] += _COLOUR_LABEL_HINT
                break
            after[key] = _ja(got)
            # compared as JSON forms: `current` may be a live reference that set_value changed
            changed = not _attr_equal(after[key], before[key])
            if changed:
                revert_values[key] = before[key]
            if not _written_equal(key, got, want):
                if changed and _is_resolution(want):
                    gaps = ', '.join(
                        f + ' (asked ' + repr(a) + ', read back ' + repr(r) + ')'
                        for f, a, r in _resolution_gaps(got, want)
                    )
                    failed[key] = (
                        'partial: set_value raised nothing but only part of the resolution took '
                        'effect; did not take: ' + gaps + '; read back ' + repr(after[key])
                    )
                elif changed:
                    failed[key] = (
                        'partial: set_value raised nothing but the value read back is '
                        + repr(after[key])
                        + ', not '
                        + repr(_ja(want))
                        + ' (it was '
                        + repr(before[key])
                        + ')'
                    )
                else:
                    failed[key] = (
                        'silent_noop: set_value raised nothing but the value read back is '
                        + repr(after[key])
                        + ', not '
                        + repr(_ja(want))
                    )
                break
            applied.append(key)
    return {
        'before': before,
        'after': after,
        'applied': applied,
        'failed': failed,
        'revertValues': revert_values,
        'changedKeys': list(revert_values),
        'conflicts': conflicts,
    }


def _set_result(op_name, obj, out, stable):
    revert = None
    if stable is not None and out['revertValues']:
        revert = {'op': op_name, 'args': {'target': stable, 'values': out['revertValues']}}
    return {
        'type': type(obj).__name__,
        'before': out['before'],
        'after': out['after'],
        'applied': out['applied'],
        'changedKeys': out['changedKeys'],
        'failed': out['failed'],
        'conflicts': out['conflicts'],
        'revert': revert,
    }


def _set_partial(out):
    if not out['failed']:
        return None
    return {'applied': len(out['applied']), 'failed': len(out['failed'])}


def _set_raise_if_failed(out):
    failed = out['failed']
    if not failed:
        return
    if out['conflicts']:
        detail = '; '.join(k + ': ' + failed[k] for k in failed)
        raise _OpError(
            'CONFLICT',
            'set_attributes: nothing written, the value changed between the read and the '
            'write (' + detail + ')',
        )
    reasons = list(failed.values())
    if any(r.startswith('silent_noop') for r in reasons):
        code = 'SILENT_NOOP'
    elif any(r.startswith('rejected') for r in reasons):
        code = 'INVALID_ARGS'
    else:
        code = 'FLAME_API_ERROR'
    detail = '; '.join(k + ': ' + failed[k] for k in failed)
    raise _OpError(
        code,
        'set_attributes: applied '
        + str(len(out['applied']))
        + ', failed '
        + str(len(failed))
        + ' (' + detail + ')',
    )


def _wiretap_selector(obj):
    try:
        wid = obj.get_wiretap_node_id()
    except BaseException:
        return None
    if not isinstance(wid, str) or wid == '':
        return None
    return {'kind': 'wiretap', 'id': wid}


def _batch_selector(batch):
    try:
        uid = _v(batch.uid)
    except BaseException:
        return None
    if not isinstance(uid, str) or uid == '':
        return None
    return {'kind': 'batch', 'uid': uid}


def _stable_desktop(obj):
    if type(obj).__name__ == 'PyBatch':
        return _batch_selector(obj)
    return _wiretap_selector(obj)


def _stable_node(node, batch):
    bsel = _batch_selector(batch)
    try:
        name = _v(node.name)
    except BaseException:
        return None
    if bsel is None or not isinstance(name, str) or name == '':
        return None
    out = {'kind': 'node', 'batch': bsel, 'name': name}
    names = _try(lambda: [_v(n.name) for n in list(batch.nodes)])
    if names is not None and names.count(name) > 1:
        # several nodes of one name: the address needs pos (None when it cannot be read as ints)
        pos = _node_pos(node)
        if pos is None or not all(isinstance(p, int) for p in pos):
            return None
        out['pos'] = pos
    return out


def _stable_action_node(inner, action, batch):
    nsel = _stable_node(action, batch)
    try:
        name = _v(inner.name)
    except BaseException:
        return None
    if nsel is None or not isinstance(name, str) or name == '':
        return None
    return {'kind': 'action_node', 'node': nsel, 'name': name}


def _stable_segment(parent, version, track, index, record_in=None, expect=None):
    # the address keeps the form it was given in (index or record_in) and its safeguard
    wsel = _wiretap_selector(parent)
    if wsel is None:
        return None
    out = {'kind': 'segment', 'parent': wsel, 'version': version, 'track': track}
    if record_in is not None:
        out['record_in'] = record_in
    else:
        out['index'] = index
    if expect is not None:
        out['expect_source_name'] = expect
    return out
""".replace("@CASE_FOLDED@", repr(CASE_FOLDED_ATTRIBUTES))
"""Shared by every `*.get_attributes` / `*.set_attributes` op. `_stable_*` build
the selector a `revert` uses: it is computed *after* the writes, so a renamed
object is addressed by its new name / wiretap id."""
