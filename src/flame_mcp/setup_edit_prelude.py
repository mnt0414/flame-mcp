"""Prelude source for `segment.edit_effect_setup` and `node.edit_setup` (`ops/segment_effects.py`,
`ops/node_edit_setup.py`) and for `segment.read_effect_setup` and `node.read_setup`
(`ops/setup_read.py`).

Real Python source that `execution.build_exec_code` places ahead of an op's own code, so each op's
`to_python` output is a few loop-free lines. It runs inside Flame (3.11) after
`timeline_edit_prelude` and `node_prelude`, whose `_te_*` and `_node_*` helpers it calls.

The whole edit runs in ONE call inside Flame: save the setup into a work directory, read it, replace
the values, write the edited file, load it, save the setup again and read that back. It cannot be
split into a server step between the save and the load, because a step of `plan.run` is one call to
Flame. The text functions are `setup_edit.py`, sent as source text (`inspect.getsource`) and run in
a namespace of their own, so the server checks a request with the same code that edits and none of
their names joins the namespace every op shares; only the `_se_` names bound below do.

What it encodes:
  - the setup of a Timewarp effect and of a Timewarp node is ONE file. An effect setup of more
    files (Burn-in Letterbox two) is refused. A node setup of more files (a Render node writes
    four, a Clip node six) is edited only when `file` names the node's own file
    (`_node_save_setup`'s `path`): `edited.<ext>` is written next to the saved `original.*`
    companions it refers to by name, and the read-back's `readback.<ext>` references are put back
    to `original.<ext>` before it is judged. Whether Flame reads those companions on the load is
    not measured; `otherFilesChanged` reports the companions whose bytes differ.
  - the work directory is `<journal dir>/setups/s_<id>`, made private (0700; the files 0600,
    because Flame writes its files 0666), and removed after a success unless `keep_files` is set.
    A failure keeps the files it wrote, and so does a refusal when `keep_files` is set, so that
    the result can say where they are. Making a new one removes the old ones (`_se_sweep`).
  - everything after the edited file is written is inside one `try`: an exception there ends in
    FLAME_API_ERROR with the files and `ran` kept in the result.
  - the two read ops (`_te_read_setup`, `_se_node_read`) save the setup into a new work directory,
    read every file of it as text (a file that is not UTF-8 is named in `notText`) and take each
    tag's value over all the files; the directory is removed whatever happened (`finally`), and
    a directory that could not be removed is named in `cleanupError`. They keep no files.
  - the read-back is judged tag by tag; a difference in another tag is reported in
    `otherDifferences` and in `degradedNotes` (which the envelope turns into `degraded`), and does
    not fail the call.
"""

from __future__ import annotations

import inspect

from flame_mcp import setup_edit

_LOADER = """
_se_lib = {}
exec(compile(_se_source, '<setup_edit>', 'exec'), _se_lib)
_se_check_values = _se_lib['check_values']
_se_count_tags = _se_lib['count_tags']
_se_tag_problems = _se_lib['tag_problems']
_se_scalar_value = _se_lib['scalar_value']
_se_check_tags = _se_lib['check_tags']
_se_read_tags = _se_lib['read_tags']
_se_format_value = _se_lib['format_value']
_se_replace_tags = _se_lib['replace_tags']
_se_values_match = _se_lib['values_match']
_se_scalar_differences = _se_lib['scalar_differences']
_se_max_age_days = _se_lib['WORKDIR_MAX_AGE_DAYS']
"""

_FLAME_SIDE = """
def _se_private_dirs(path):
    import os
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)


def _se_sweep(base):
    # remove the work directories this feature made more than a few days ago: only 's_<12 hex>'
    # directories, only the plain files in them, nothing that is a link
    import os
    import time
    cutoff = time.time() - _se_max_age_days * 86400
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        path = os.path.join(base, name)
        hexdigits = '0123456789abcdef'
        if not (name.startswith('s_') and len(name) == 14):
            continue
        if any(ch not in hexdigits for ch in name[2:]):
            continue
        try:
            if os.path.islink(path) or not os.path.isdir(path) or os.path.getmtime(path) >= cutoff:
                continue
            for entry in os.listdir(path):
                entry_path = os.path.join(path, entry)
                if os.path.isfile(entry_path) and not os.path.islink(entry_path):
                    os.remove(entry_path)
            os.rmdir(path)
        except OSError:
            pass


def _se_new_workdir(base):
    import os
    import uuid
    _se_private_dirs(base)
    _se_sweep(base)
    workdir = os.path.join(base, 's_' + uuid.uuid4().hex[:12])
    os.mkdir(workdir, 0o700)
    os.chmod(workdir, 0o700)
    return workdir


def _se_tighten(paths):
    import os
    for path in paths:
        os.chmod(path, 0o600)


def _se_read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _se_read_text(path):
    try:
        return _se_read_bytes(path).decode('utf-8')
    except UnicodeDecodeError:
        return None


def _se_write_text(path, text):
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(text.encode('utf-8'))
    os.chmod(path, 0o600)


def _se_remove_workdir(out, workdir):
    import os
    try:
        for name in os.listdir(workdir):
            os.remove(os.path.join(workdir, name))
        os.rmdir(workdir)
    except BaseException as exc:
        out['cleanupError'] = type(exc).__name__ + ': ' + str(exc)


def _se_discard(out, keep_files):
    # the look ended (it failed, or it was only a check): remove the work directory and do not
    # name it in the result, unless the files are to be kept for a look
    workdir = out.get('workDir')
    if workdir is None:
        return
    if keep_files:
        out['filesKept'] = True
        return
    out.pop('workDir', None)
    out.pop('files', None)
    _se_remove_workdir(out, workdir)


def _se_one_file(out, paths, what):
    # the files a save wrote, as the one file that is edited; the failure otherwise
    out['savedFiles'] = list(paths)
    if len(paths) == 1:
        return True
    names = repr([p.rsplit('/', 1)[-1] for p in paths])
    if len(paths) == 0:
        _te_fail(out, 'FLAME_API_ERROR', what + ' wrote no file')
    else:
        _te_fail(
            out,
            'PRECONDITION_UNMET',
            what + ' wrote ' + str(len(paths)) + ' files ' + names + ' and which of them is the '
            'setup to edit could not be told, so none is edited',
        )
    return False


def _se_inspect(out, text, values):
    # count every tag in the saved setup; refuse what cannot be replaced or would change nothing
    tags = list(values)
    out['tagCounts'] = _se_count_tags(text, tags)
    bad = _se_tag_problems(text, tags)
    if len(bad) > 0:
        out['tagProblems'] = bad
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'only a tag that occurs exactly once as <TAG>value</TAG> can be replaced: '
            + '; '.join(t + ': ' + bad[t] for t in tags if t in bad),
        )
    rows = {}
    unchanged = []
    for tag in tags:
        have = _se_scalar_value(text, tag)
        rows[tag] = {'before': have, 'requested': _se_format_value(values[tag]), 'after': None}
        if _se_values_match(values[tag], have):
            unchanged.append(tag)
    out['tags'] = rows
    out['unchanged'] = unchanged
    if len(unchanged) == len(tags):
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'nothing to change: ' + ', '.join(tags) + ' already hold the value asked for; '
            'nothing was written',
        )
    return out


def _se_edited_text(text, values, unchanged):
    changing = {}
    for tag in values:
        if tag not in unchanged:
            changing[tag] = values[tag]
    return _se_replace_tags(text, changing)


def _se_judge(out, values, edited_text, readback_text):
    # the values read back against the ones asked for; the rest of the file against what was loaded
    tags = list(values)
    bad = _se_tag_problems(readback_text, tags)
    mismatched = []
    for tag in tags:
        after = None if tag in bad else _se_scalar_value(readback_text, tag)
        out['tags'][tag]['after'] = after
        if not _se_values_match(values[tag], after):
            mismatched.append(tag)
    out['mismatched'] = mismatched
    if len(mismatched) > 0:
        return _te_fail(
            out,
            'FLAME_API_ERROR',
            'the setup was loaded, but the values read back differ from the ones asked for: '
            + '; '.join(
                t + ' is ' + repr(out['tags'][t]['after']) + ', asked for '
                + repr(out['tags'][t]['requested'])
                for t in mismatched
            ),
        )
    if readback_text != edited_text:
        differences = _se_scalar_differences(edited_text, readback_text, tags)
        out['otherDifferences'] = differences
        where = (
            str(len(differences)) + ' tag(s) besides the ones asked for (' + ', '.join(differences)
            + ')' if len(differences) > 0
            else 'the way the tags asked for are written, or outside the tags'
        )
        out['degradedNotes'] = [
            'the setup saved for the read-back differs from the file that was loaded in ' + where
            + '; the values asked for are in it'
        ]
    return out


def _se_finish(out, workdir, keep_files):
    if out['error'] is None and not keep_files:
        out['filesKept'] = False
        out.pop('workDir', None)
        out.pop('files', None)
        _se_remove_workdir(out, workdir)
    else:
        out['filesKept'] = True
    return out


def _se_stopped(out, exc):
    # an exception after the edited file was written: the load may have taken effect
    return _te_fail(
        out,
        'FLAME_API_ERROR',
        'the edit stopped after the edited setup file was written (the load may have taken '
        'effect; journal.revert restores the checkpoint): ' + type(exc).__name__ + ': ' + str(exc),
    )


def _te_edit_look(seg, parent, fx_type, values, base):
    # save the effect's setup into a new work directory and look at it; (out, held) where held is
    # (the text, the saved file) and out['error'] is None when the edit can go on
    import os
    out = _te_start(seg, parent, 'edit the setup of', True)
    if out['error'] is not None:
        return out, None
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, ' (segment.create_effect first)'), None
    out['type'] = _j(fx.type)
    problems = _se_check_values(values)
    if len(problems) > 0:
        return _te_fail(out, 'INVALID_ARGS', '; '.join(problems)), None
    workdir = _se_new_workdir(base)
    out['workDir'] = workdir
    out['files'] = {'original': None, 'edited': None, 'readback': None}
    try:
        saved = _te_save_setup(seg, parent, fx_type, os.path.join(workdir, 'original'), False)
        if saved['error'] is not None:
            code = saved['errorCode']
            return _te_fail(
                out, 'FLAME_API_ERROR' if code == 'SILENT_NOOP' else code, saved['error']
            ), None
        paths = [f['path'] for f in saved['files']]
        _se_tighten(paths)
        if not _se_one_file(out, paths, 'save_setup'):
            return out, None
        out['files']['original'] = paths[0]
        text = _se_read_text(paths[0])
        if text is None:
            return _te_fail(
                out, 'PRECONDITION_UNMET', 'the setup file is not UTF-8 text: it cannot be edited'
            ), None
        return _se_inspect(out, text, values), (text, paths[0])
    except BaseException:
        _se_remove_workdir(out, workdir)
        raise


def _te_edit_setup_check(seg, parent, fx_type, values, keep_files, base):
    out, held = _te_edit_look(seg, parent, fx_type, values, base)
    _se_discard(out, keep_files and out['error'] is not None)
    return out


def _te_edit_after_write(seg, parent, version, track, fx_type, values, out, edited, edited_text):
    import os
    loaded = _te_load_setup(seg, parent, version, track, fx_type, edited)
    for key in ('path', 'size', 'returned', 'exception', 'attributes', 'changedKeys',
                'trackChanged', 'position', 'effects'):
        out[key] = loaded.get(key)
    if loaded['error'] is not None:
        return _te_fail(out, loaded['errorCode'], loaded['error'])
    again = _te_save_setup(
        seg, parent, fx_type, os.path.join(os.path.dirname(edited), 'readback'), False
    )
    if again['error'] is not None:
        return _te_fail(
            out, 'FLAME_API_ERROR',
            'the setup was loaded, but it could not be saved again to read it back: '
            + again['error'],
        )
    paths = [f['path'] for f in again['files']]
    _se_tighten(paths)
    if not _se_one_file(out, paths, 'the read-back save_setup'):
        return out
    out['files']['readback'] = paths[0]
    back = _se_read_text(paths[0])
    if back is None:
        return _te_fail(
            out, 'FLAME_API_ERROR', 'the setup saved for the read-back is not UTF-8 text'
        )
    return _se_judge(out, values, edited_text, back)


def _te_edit_setup(seg, parent, version, track, fx_type, values, keep_files, base):
    import os
    out, held = _te_edit_look(seg, parent, fx_type, values, base)
    if out['error'] is not None:
        _se_discard(out, keep_files)
        return out
    workdir = out['workDir']
    text, original = held
    edited_text = _se_edited_text(text, values, out['unchanged'])
    edited = os.path.join(workdir, 'edited' + os.path.basename(original)[len('original'):])
    _se_write_text(edited, edited_text)
    out['files']['edited'] = edited
    out['ran'] = True
    try:
        _te_edit_after_write(seg, parent, version, track, fx_type, values, out, edited, edited_text)
    except BaseException as exc:
        _se_stopped(out, exc)
    return _se_finish(out, workdir, keep_files)


def _se_node_again(batch, node, index, count_before):
    # the node after a load_node_setup, which may have replaced it: the same object, else the one
    # at the same place of the group, else None
    try:
        nodes = list(batch.nodes)
        for candidate in nodes:
            if candidate is node:
                return node
        if index is not None and len(nodes) == count_before:
            return nodes[index]
    except BaseException:
        pass
    return None


def _se_ext_of(path, stem):
    # the extension of a saved file: 'output_node' for 'original.output_node'
    return path.rsplit('/', 1)[-1][len(stem) + 1:]


def _se_choose_file(out, saved, paths, file):
    # which of the files a node save wrote is edited: its path, or None with the failure in out
    exts = [_se_ext_of(p, 'original') for p in paths]
    main = saved['path']
    if file is not None and file not in exts:
        _te_fail(
            out, 'INVALID_ARGS',
            'file=' + repr(file) + ' is not one of the files the node saved: ' + repr(exts),
        )
        return None
    if len(paths) == 1:
        return paths[0]
    main_ext = _se_ext_of(main, 'original')
    if file is None:
        _te_fail(
            out, 'PRECONDITION_UNMET',
            'save_node_setup wrote ' + str(len(paths)) + ' files with the extensions ' + repr(exts)
            + ': name the own file of the node with file=' + repr(main_ext),
        )
        return None
    if file != main_ext:
        _te_fail(
            out, 'PRECONDITION_UNMET',
            'only the own file of the node (' + main_ext + ') can be edited; the other files are '
            'left as saved',
        )
        return None
    return main


def _se_node_look(batch, node, values, file, base):
    import os
    out = {
        'ran': False,
        'error': None,
        'errorCode': None,
        'node': _node_row(node, _name_counts(_node_names(batch))),
    }
    problems = _se_check_values(values)
    if len(problems) > 0:
        return _te_fail(out, 'INVALID_ARGS', '; '.join(problems)), None
    workdir = _se_new_workdir(base)
    out['workDir'] = workdir
    out['files'] = {'original': None, 'edited': None, 'readback': None}
    try:
        saved = _node_save_setup(batch, node, os.path.join(workdir, 'original'), False)
        paths = list(saved['files'])
        _se_tighten(paths)
        if len(paths) == 0 or saved['path'] is None:
            _se_one_file(out, paths, 'save_node_setup')
            if saved['error'] is not None:
                if len(paths) == 0:
                    out['error'] = saved['error']
                else:
                    out['error'] = out['error'] + ' (' + saved['error'] + ')'
            return out, None
        out['savedFiles'] = paths
        if saved['error'] is not None:
            return _te_fail(out, 'FLAME_API_ERROR', saved['error']), None
        main = _se_choose_file(out, saved, paths, file)
        if main is None:
            return out, None
        if file is not None:
            out['file'] = file
        out['files']['original'] = main
        text = _se_read_text(main)
        if text is None:
            return _te_fail(
                out, 'PRECONDITION_UNMET', 'the setup file is not UTF-8 text: it cannot be edited'
            ), None
        return _se_inspect(out, text, values), (text, main)
    except BaseException:
        _se_remove_workdir(out, workdir)
        raise


def _se_node_edit_check(batch, node, values, file, keep_files, base):
    out, held = _se_node_look(batch, node, values, file, base)
    _se_discard(out, keep_files and out['error'] is not None)
    return out


def _se_files_changed(originals, readbacks):
    # readbacks: extension -> path of the second save's companion files. The extensions of the
    # companion files whose bytes differ between the two saves, or that only one of them wrote
    before = {}
    for path in originals:
        before[_se_ext_of(path, 'original')] = path
    changed = []
    for ext in list(before) + [e for e in readbacks if e not in before]:
        if (
            ext not in before
            or ext not in readbacks
            or _se_read_bytes(before[ext]) != _se_read_bytes(readbacks[ext])
        ):
            changed.append(ext)
    return changed


def _se_node_after_write(batch, node, values, out, edited, edited_text):
    import os
    nodes_before = list(batch.nodes)
    index = _node_index(nodes_before, node)
    loaded = _node_load_setup(batch, node, edited)
    out['loadReturned'] = loaded['loadReturned']
    out['changedKeys'] = loaded['changedKeys']
    out['noChange'] = loaded['noChange']
    if loaded['error'] is not None:
        return _te_fail(out, loaded['errorCode'] or 'FLAME_API_ERROR', loaded['error'])
    fresh = _se_node_again(batch, node, index, len(nodes_before))
    if fresh is None:
        return _te_fail(
            out, 'FLAME_API_ERROR',
            'the setup was loaded, but the node cannot be found again to read it back',
        )
    again = _node_save_setup(batch, fresh, os.path.join(os.path.dirname(edited), 'readback'), False)
    paths = list(again['files'])
    _se_tighten(paths)
    if again['error'] is not None or again['path'] is None:
        out['savedFiles'] = paths
        return _te_fail(
            out, 'FLAME_API_ERROR',
            'the setup was loaded, but it could not be saved again to read it back: '
            + str(again['error'] or (str(len(paths)) + ' files')),
        )
    out['files']['readback'] = again['path']
    back = _se_read_text(again['path'])
    if back is None:
        return _te_fail(
            out, 'FLAME_API_ERROR', 'the setup saved for the read-back is not UTF-8 text'
        )
    originals = [p for p in out['savedFiles'] if p != out['files']['original']]
    companions = {_se_ext_of(p, 'readback'): p for p in paths if p != again['path']}
    for ext in companions:
        back = back.replace('readback.' + ext, 'original.' + ext)
    _se_judge(out, values, edited_text, back)
    if len(originals) > 0:
        out['otherFilesChanged'] = _se_files_changed(originals, companions)
    out['node'] = _node_row(fresh, _name_counts(_node_names(batch)))
    return out


def _se_node_edit(batch, node, values, file, keep_files, base):
    import os
    out, held = _se_node_look(batch, node, values, file, base)
    if out['error'] is not None:
        _se_discard(out, keep_files)
        return out
    workdir = out['workDir']
    text, original = held
    edited_text = _se_edited_text(text, values, out['unchanged'])
    edited = os.path.join(workdir, 'edited' + os.path.basename(original)[len('original'):])
    _se_write_text(edited, edited_text)
    out['files']['edited'] = edited
    out['ran'] = True
    try:
        _se_node_after_write(batch, node, values, out, edited, edited_text)
    except BaseException as exc:
        _se_stopped(out, exc)
    return _se_finish(out, workdir, keep_files)


def _se_file_ident(path):
    # the part of a saved file's name after 'setup': '.timewarp_node', '.lut_node'
    name = path.rsplit('/', 1)[-1]
    return name[len('setup'):] if name.startswith('setup') else name


def _se_read_files(out, paths, tags):
    # read the files a save wrote into the work directory and take the value of each tag over all
    # of them; a file that is not UTF-8 is named in notText and not read
    _se_tighten(paths)
    texts = {}
    not_text = []
    for path in paths:
        ident = _se_file_ident(path)
        text = _se_read_text(path)
        if text is None:
            not_text.append(ident)
        else:
            texts[ident] = text
    out['savedFiles'] = [_se_file_ident(p) for p in paths]
    out['notText'] = not_text
    if len(texts) == 0:
        return _te_fail(
            out,
            'PRECONDITION_UNMET',
            'none of the setup files is UTF-8 text (' + ', '.join(not_text) + '): they cannot be '
            'read',
        )
    got = _se_read_tags(texts, tags)
    for tag in tags:
        if got['tagCounts'][tag] == 0 and len(not_text) > 0:
            got['problems'][tag] += ' (files not read as text: ' + ', '.join(not_text) + ')'
    out.update(
        values=got['values'],
        problems=got['problems'],
        tagCounts=got['tagCounts'],
        fileOf=got['fileOf'],
    )
    return out


def _te_read_setup(seg, parent, fx_type, tags, base):
    # save the effect's setup into a new work directory, read the tags, remove the directory
    import os
    out = _te_start(seg, parent, 'read the setup of', False)
    out.pop('before', None)
    out.pop('ran', None)
    if out['error'] is not None:
        return out
    out['requestedType'] = fx_type
    fx = _te_fx_find(seg, fx_type)
    if fx is None:
        return _te_fx_missing(out, seg, fx_type, '')
    out['type'] = _j(fx.type)
    problems = _se_check_tags(tags)
    if len(problems) > 0:
        return _te_fail(out, 'INVALID_ARGS', '; '.join(problems))
    workdir = _se_new_workdir(base)
    try:
        saved = _te_save_setup(seg, parent, fx_type, os.path.join(workdir, 'setup'), False)
        if saved['error'] is not None:
            code = saved['errorCode']
            _te_fail(out, 'FLAME_API_ERROR' if code == 'SILENT_NOOP' else code, saved['error'])
        else:
            _se_read_files(out, [f['path'] for f in saved['files']], tags)
    finally:
        _se_remove_workdir(out, workdir)
    return out


def _se_node_read(batch, node, tags, base):
    # save the node's setup into a new work directory, read the tags, remove the directory
    import os
    out = {
        'error': None,
        'errorCode': None,
        'node': _node_row(node, _name_counts(_node_names(batch))),
    }
    problems = _se_check_tags(tags)
    if len(problems) > 0:
        return _te_fail(out, 'INVALID_ARGS', '; '.join(problems))
    workdir = _se_new_workdir(base)
    try:
        saved = _node_save_setup(batch, node, os.path.join(workdir, 'setup'), False)
        # an error that only says no file is the one named after the node type is not one here:
        # every file is read
        if len(saved['files']) == 0 or saved['saveSetupReturned'] is not True:
            _te_fail(out, 'FLAME_API_ERROR', saved['error'])
        else:
            _se_read_files(out, list(saved['files']), tags)
    finally:
        _se_remove_workdir(out, workdir)
    return out
"""

SETUP_EDIT_HELPER = (
    "_se_source = " + repr(inspect.getsource(setup_edit)) + "\n" + _LOADER + "\n" + _FLAME_SIDE
)
"""The source of `setup_edit.py` as a string, the lines that run it in a namespace of its own, and
the Flame-side code above, as one block of source."""
