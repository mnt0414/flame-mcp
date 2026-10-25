"""Prelude source for the walk over the whole project (`desktop.list_all`, `ops/desktop_list.py`).

Like `read_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code. It runs inside Flame (3.11) with `flame`, `_v`, `_try`, `_rp_try` and `_rp_find_item`
already defined, and it never imports `flame` or `PySide6` itself.

What `_ip_walk(scope, types, budget_s)` does:
  - the order is fixed: each Desktop reel group (its reels, their clips and sequences), then the
    Desktop's Batch groups, then each library of the workspace (its folders at any depth, their
    reels, the clips and sequences). A container is listed before what is inside it.
  - a Batch group is listed by name and uid and counted in `batchGroupsSkipped`; what is inside it
    (nodes, reels, shelf reels) is never read.
  - a library whose `opened` does not read True (False, or unreadable) is listed and named in
    `closedLibraries`; it is never opened and its children are not read (a closed library reads
    as empty).
  - `types` keeps the entries of the named Flame types; containers are walked into whatever the
    filter. An entry that could not be read (a container whose children raise) is an entry of its
    own, `{'error': ...}`, that no filter drops.
  - the time budget is checked before each container's children are read, except the first: past
    the budget the walk stops and `stoppedEarly` says how many containers were read.
"""

from __future__ import annotations

from typing import Final

INSPECT_BUDGET_S: Final[float] = 5.0
"""The walk stops reading containers after this long (the first container is always read)."""

INSPECT_HELPER = """
def _ip_name(obj):
    return _try(lambda: _v(obj.name))


def _ip_over_budget(st):
    if st['stopped'] is not None:
        return True
    if st['reads'] == 0:
        return False
    import time as _ip_time
    if _ip_time.monotonic() - st['started'] > st['budget']:
        st['stopped'] = {'reason': 'time budget', 'budgetS': st['budget'], 'read': st['reads']}
        return True
    return False


def _ip_add(st, obj, path, location, library):
    kind = type(obj).__name__
    if kind == 'PyBatch':
        st['batch'] += 1
    if st['types'] and kind not in st['types']:
        return
    st['entries'].append(
        {'obj': obj, 'path': path, 'location': location, 'library': library}
    )


def _ip_add_error(st, exc, path, location, library):
    st['entries'].append(
        {
            'error': str(exc) or type(exc).__name__,
            'path': path,
            'location': location,
            'library': library,
        }
    )


def _ip_children_of(obj, kind):
    if kind == 'PyReelGroup':
        return list(obj.reels)
    return list(obj.children)


def _ip_visit(st, obj, path, location, library):
    kind = type(obj).__name__
    here = path + [_ip_name(obj)]
    if kind == 'PyLibrary':
        library = here[-1]
    _ip_add(st, obj, here, location, library)
    if kind == 'PyLibrary' and _try(lambda: _v(obj.opened)) is not True:
        st['closed'].append(here[-1])
        return
    if kind not in ('PyReelGroup', 'PyReel', 'PyFolder', 'PyLibrary'):
        return
    if _ip_over_budget(st):
        return
    st['reads'] += 1
    try:
        kids = _ip_children_of(obj, kind)
    except BaseException as exc:
        _ip_add_error(st, exc, here, location, library)
        return
    for kid in kids:
        if st['stopped'] is not None:
            return
        _ip_visit(st, kid, here, location, library)


def _ip_visit_all(st, getter, location):
    try:
        objs = list(getter())
    except BaseException as exc:
        _ip_add_error(st, exc, [], location, None)
        return
    for obj in objs:
        if st['stopped'] is not None:
            return
        _ip_visit(st, obj, [], location, None)


def _ip_walk(scope, types, budget_s):
    import time as _ip_time
    st = {
        'entries': [],
        'closed': [],
        'batch': 0,
        'reads': 0,
        'stopped': None,
        'types': list(types),
        'budget': budget_s,
        'started': _ip_time.monotonic(),
    }
    workspace = flame.projects.current_project.current_workspace
    if scope in ('all', 'desktop'):
        desktop = workspace.desktop
        _ip_visit_all(st, lambda: desktop.reel_groups, 'desktop')
        _ip_visit_all(st, lambda: desktop.batch_groups, 'desktop')
    if scope in ('all', 'libraries'):
        _ip_visit_all(st, lambda: workspace.libraries, 'library')
    return {
        'entries': st['entries'],
        'closedLibraries': st['closed'],
        'batchGroupsSkipped': st['batch'],
        'stoppedEarly': st['stopped'],
    }


def _ip_item(entry):
    if 'error' in entry:
        item = {'error': entry['error']}
    else:
        item = _rp_try(lambda: _rp_find_item(entry['obj']))
    item['path'] = entry['path']
    item['location'] = entry['location']
    item['library'] = entry['library']
    return item
"""
