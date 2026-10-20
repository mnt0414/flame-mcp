"""Prelude source for opening a closed library around an op.

Like `desktop_prelude`: real Python source that `execution.build_exec_code` places ahead of an op's
own code. It runs inside Flame (3.11) with `flame`, `_v`, `_ja`, `_try` and `_OpError` defined.

What it encodes (measured on Flame 2025.2.4):
  - a library that is closed reads `opened` False and lists nothing (its `children` count 0 although
    it holds something): what it holds must never be judged from such a read.
  - reading the name and uid in the list of libraries (`workspace.libraries`) does not open it,
    and neither does waiting. `flame.find_by_wiretap_node_id(id)` of the library, or of anything
    inside it, opens it. `PyLibrary.close()` answers True and closes it; `open()` opens it.
  - so before an op resolves its selectors, the libraries it names are found in the list by the
    uid in a wiretap id (an id is a path of uids) or by the first name of a path selector, a
    closed one is opened (`_lo_open`), and after the op (a failure included) only the ones this
    call opened are closed again (`_lo_close`) and told in the result (`_lo_report`:
    `librariesOpened` and a note). A library that cannot be opened (open() fails, or `opened`
    does not read True afterwards) or whose `opened` cannot be read stops the call before
    anything is done (`PRECONDITION_UNMET`). A library that cannot be closed again is told in the
    result and left open (nothing is lost: it is not an error of the op).
"""

from __future__ import annotations

LIBRARY_OPEN_HELPER = """
def _lo_matches(lib, refs):
    uid = _try(lambda: _v(lib.uid))
    name = _try(lambda: _v(lib.name))
    if uid is not None and uid in refs['uids']:
        return True
    if name is not None and name in refs['names']:
        return True
    if uid is not None:
        for w in refs['wiretapIds']:
            if uid in str(w).split('/'):
                return True
    return False


def _lo_opened(lib):
    return _try(lambda: _v(lib.opened))


def _lo_open(refs, state):
    if not (refs['uids'] or refs['names'] or refs['wiretapIds']):
        return
    for lib in list(flame.projects.current_project.current_workspace.libraries):
        if not _lo_matches(lib, refs):
            continue
        name = _try(lambda lib=lib: _v(lib.name))
        uid = _try(lambda lib=lib: _v(lib.uid))
        was = _lo_opened(lib)
        if was is True:
            continue
        if was is not False:
            raise _OpError(
                'PRECONDITION_UNMET',
                'whether the library ' + repr(name) + ' is open could not be read: nothing was '
                'done',
            )
        try:
            returned = lib.open()
        except BaseException as exc:
            raise _OpError(
                'PRECONDITION_UNMET',
                'the library ' + repr(name) + ' is closed and could not be opened ('
                + type(exc).__name__ + ': ' + str(exc) + '): nothing was done',
            )
        if _lo_opened(lib) is not True:
            raise _OpError(
                'PRECONDITION_UNMET',
                'the library ' + repr(name) + ' is closed and could not be opened (open() '
                'returned ' + repr(_ja(returned)) + ', opened reads ' + repr(_lo_opened(lib))
                + '): nothing was done',
            )
        state['opened'].append({'name': name, 'uid': uid, 'closedAgain': None})


def _lo_find(uid):
    for lib in list(flame.projects.current_project.current_workspace.libraries):
        if _try(lambda lib=lib: _v(lib.uid)) == uid:
            return lib
    return None


def _lo_close(state):
    for row in state['opened']:
        lib = _lo_find(row['uid'])
        if lib is None:
            # the op deleted the library: nothing to close
            row['deleted'] = True
            row['closedAgain'] = None
            continue
        try:
            returned = lib.close()
        except BaseException as exc:
            returned = None
            row['error'] = type(exc).__name__ + ': ' + str(exc)
        row['closedAgain'] = _lo_opened(lib) is False
        if not row['closedAgain']:
            row['closeReturned'] = _ja(returned)


def _lo_notes_of(value):
    notes = value.get('notes')
    if isinstance(notes, str):
        return [notes]
    if isinstance(notes, (list, tuple)):
        return list(notes)
    return []


def _lo_report(value, state):
    if not state['opened']:
        return value
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return value
    value['librariesOpened'] = state['opened']
    notes = _lo_notes_of(value)
    for row in state['opened']:
        if row.get('deleted'):
            notes.append(
                'the library ' + repr(row['name']) + ' was opened for this call and is deleted '
                'now'
            )
        elif row['closedAgain']:
            notes.append(
                'the library ' + repr(row['name']) + ' was closed: it was opened for this call '
                'and closed again'
            )
        else:
            notes.append(
                'the library ' + repr(row['name']) + ' could not be closed again: it was opened '
                'for this call and is left open (nothing is lost; close it by hand)'
            )
    value['notes'] = notes
    return value


def _lo_finish(value, state):
    # runs after the op, whatever happened: an exception here must never cost the op's own result
    problem = None
    try:
        _lo_close(state)
    except BaseException as exc:
        problem = (
            'closing the libraries this call opened failed: ' + type(exc).__name__ + ': ' + str(exc)
        )
    for row in state['opened']:
        if 'closedAgain' not in row or (row['closedAgain'] is None and not row.get('deleted')):
            row['closedAgain'] = False
    try:
        value = _lo_report(value, state)
    except BaseException as exc:
        problem = (
            'reporting the libraries this call opened failed: ' + type(exc).__name__ + ': '
            + str(exc)
        )
    if problem is not None:
        try:
            if value is None:
                value = {}
            if isinstance(value, dict):
                value['notes'] = _lo_notes_of(value) + [problem]
        except BaseException:
            pass
    return value


def _describe_library(obj):
    entry = _describe_entry(obj)
    entry['opened'] = _try(lambda: _v(obj.opened))
    return entry
"""
