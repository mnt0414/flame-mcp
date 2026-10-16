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
  - compare-and-swap: when the server passes `expected` (the values it read a
    moment ago), every key's current value must still equal it, checked after the
    preflight and before the first write; one mismatch means nothing is written and
    the call ends in CONFLICT (this closes the gap between the server's read and the write,
    in which a user could change the value in the UI).
"""

from __future__ import annotations

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


def _ja(x):
    v = _v(x)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_ja(i) for i in v]
    if type(v).__name__ == 'PyTime':
        return {'timecode': _ja(v.timecode), 'frame': _ja(v.frame)}
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


def _attr_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return abs(a - b) <= 1e-6
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_attr_equal(x, y) for x, y in zip(a, b))
    return a == b


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
        return 'rejected: PyTime attributes are not supported yet', None, current
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
                break
            after[key] = _ja(got)
            if not _attr_equal(got, current):
                revert_values[key] = before[key]
            if not _attr_equal(got, want):
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
"""
"""Shared by every `*.get_attributes` / `*.set_attributes` op. `_stable_*` build
the selector a `revert` uses: it is computed *after* the writes, so a renamed
object is addressed by its new name / wiretap id."""
