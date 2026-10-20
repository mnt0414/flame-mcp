"""Prelude source for the checkpoint ops and `eval.run`.

Like `attribute_prelude.ATTRIBUTE_HELPER`, real Python source placed ahead of an op's own
code, so those ops' `to_python` output stays free of loops (registry.py's pacing guard parses
only that output). It runs inside Flame (3.11) with `flame`, `_v`, `_ja` and `_OpError`
already defined.

What it encodes:
  - a Batch group's fingerprint: sha256 over the sorted list of
    [node name, `type` attribute, input sockets -> names of the nodes wired to them]
    (`node.sockets` is `{'input': {socket: [names]}, 'output': {...}}`). Read-only.
    It sees the node set and the wiring, not attribute values or positions. An Action / Image /
    GMask Tracer node carries a fourth element: the sorted [name, type] of its inner nodes
    (`node.nodes`); every other node has the three-element entry, so a group without such
    nodes keeps the fingerprint already recorded in the journal.
  - optional sections (off unless asked for): `reels`, `shelf_reels` (each reel's name and
    the names of its clips), `iterations` (name and number) and `attributes` (every node's
    attribute values: it sees a value changed by hand, which the nodes-and-wiring
    fingerprint does not). With sections the hashed blob is
    {'nodes': [...], <section>: [...]} instead of the plain list. Which sections the server asks
    for by default is `checkpoints.DEFAULT_FINGERPRINT_SECTIONS` (empty: a restore does not
    bring back every reel and iteration as recorded, so those sections would differ after one).
  - `save_setup` rewrites the path it is given (measured on Flame 2025.2.4: a `.batch` is
    appended, dots become underscores, and a directory of the same stem is written beside it):
    the path to load later is found by globbing for what appeared, never by trusting the
    requested one.
  - a failed `load_setup` empties the group (measured on Flame 2025.2.4: `load_setup`
    of a path that did not exist raised and left the group with no nodes), so the setup file's
    existence is checked in Flame right before the call, and `load_setup` is not called when it
    is missing.
  - reel fidelity: a checkpoint also records the group's schematic reel inventory (name, empty
    or not, clip / sequence names, in `batch.reels` order) and the group's fingerprint over all
    sections, both read right before `save_setup`. Measured on Flame 2025.2.4: after
    `load_setup`, empty reels are gone, Flame's default empty reels have been
    added and reels that hold clips are rebuilt (with new uids). `_tidy_reels` runs right after
    a restore's `load_setup`: (a) it creates the recorded empty reels that are missing, then
    (b) deletes the empty reels the record does not have (`flame.delete(reel, confirm=False)`),
    and reads the all-section fingerprint back. A reel that holds a clip or a sequence is never
    deleted: deleting one also deleted the Clip nodes that use it. The order matters: deleting
    the last reel of a group made Flame create its default ones.
"""

from __future__ import annotations

CHECKPOINT_HELPER = """
def _inner_entries(n):
    # (has_inner, inner): inner is the sorted [name, type] of the inner nodes, None when unreadable
    if type(n).__name__ not in ('PyActionNode', 'PyImageNode', 'PyGMaskTracerNode'):
        return False, None
    import json
    try:
        inner = []
        for c in list(n.nodes):
            try:
                cname = _v(c.name)
            except BaseException:
                cname = None
            try:
                ctype = _ja(c.type)
            except BaseException:
                ctype = None
            inner.append([cname, ctype])
        inner.sort(key=lambda e: json.dumps(e))
        return True, inner
    except BaseException:
        return True, None


def _fp_reel_rows(batch, attr):
    import json
    rows = []
    for r in list(getattr(batch, attr)):
        try:
            rname = _v(r.name)
        except BaseException:
            rname = None
        try:
            clips = [_v(c.name) for c in list(r.clips)]
        except BaseException:
            clips = None
        try:
            sequences = [_v(c.name) for c in list(r.sequences)]
        except BaseException:
            sequences = None
        rows.append([rname, clips, sequences])
    rows.sort(key=lambda e: json.dumps(e))
    return rows


def _fp_iteration_rows(batch):
    import json
    rows = []
    for it in list(batch.batch_iterations):
        try:
            iname = _v(it.name)
        except BaseException:
            iname = None
        try:
            number = _ja(it.iteration_number)
        except BaseException:
            number = None
        rows.append([iname, number])
    rows.sort(key=lambda e: json.dumps(e))
    return rows


def _fp_attribute_rows(batch):
    # every node's attribute values (`_get_attributes`, JSON form): [node name, [[key, value]...]];
    # a value that cannot be read is recorded as its error text (the same text every time)
    import json
    rows = []
    for n in list(batch.nodes):
        try:
            nname = _v(n.name)
        except BaseException:
            nname = None
        got = _get_attributes(n, None, False)['attributes']
        values = [[key, got[key].get('value', {'error': got[key].get('error')})] for key in got]
        values.sort(key=lambda e: e[0])
        rows.append([nname, values])
    rows.sort(key=lambda e: json.dumps(e, sort_keys=True))
    return rows


def _fingerprint_sections(batch, sections):
    data = {}
    unreadable = []
    for section in sections:
        try:
            if section == 'reels':
                data[section] = _fp_reel_rows(batch, 'reels')
            elif section == 'shelf_reels':
                data[section] = _fp_reel_rows(batch, 'shelf_reels')
            elif section == 'iterations':
                data[section] = _fp_iteration_rows(batch)
            elif section == 'attributes':
                data[section] = _fp_attribute_rows(batch)
            else:
                raise ValueError('unknown fingerprint section ' + repr(section))
        except BaseException:
            data[section] = None
            unreadable.append(section)
    return data, unreadable


def _batch_fingerprint(batch, include_nodes=False, sections=None):
    import hashlib
    import json
    entries = []
    unreadable = 0
    edges = 0
    for n in list(batch.nodes):
        try:
            name = _v(n.name)
        except BaseException:
            name = None
        try:
            ntype = _ja(n.type)
        except BaseException:
            ntype = None
        inputs = None
        try:
            socks = _v(n.sockets)
            table = socks.get('input') if isinstance(socks, dict) else None
            if isinstance(table, dict):
                inputs = []
                for sock in table:
                    linked = table[sock]
                    if not isinstance(linked, (list, tuple)):
                        linked = []
                    names = sorted(str(c) for c in linked)
                    edges += len(names)
                    inputs.append([str(sock), names])
                inputs.sort(key=lambda e: e[0])
        except BaseException:
            inputs = None
        if inputs is None:
            unreadable += 1
        entry = [name, ntype, inputs]
        has_inner, inner = _inner_entries(n)
        if has_inner:
            entry.append(inner)
            if inner is None:
                unreadable += 1
        entries.append(entry)
    entries.sort(key=lambda e: json.dumps(e, sort_keys=True))
    sections = list(sections) if sections else []
    hashed = entries
    section_data = {}
    unreadable_sections = []
    if sections:
        section_data, unreadable_sections = _fingerprint_sections(batch, sections)
        hashed = dict(section_data)
        hashed['nodes'] = entries
    blob = json.dumps(hashed, sort_keys=True, separators=(',', ':')).encode('utf-8')
    out = {
        'fingerprint': hashlib.sha256(blob).hexdigest(),
        'nodeCount': len(entries),
        'connectionCount': edges,
        'unreadableNodes': unreadable,
        'sections': sections,
        'unreadableSections': unreadable_sections,
    }
    if include_nodes:
        out['nodes'] = [
            {'name': e[0], 'type': e[1], 'inputs': e[2], **({'inner': e[3]} if len(e) > 3 else {})}
            for e in entries
        ]
        if sections:
            out['sectionData'] = section_data
    return out


def _batch_ident(batch):
    try:
        uid = _v(batch.uid)
    except BaseException:
        uid = None
    try:
        name = _v(batch.name)
    except BaseException:
        name = None
    return {'uid': uid, 'name': name}


def _reel_inventory(batch):
    rows = []
    for r in list(batch.reels):
        rname = _v(r.name)
        if not isinstance(rname, str):
            raise ValueError('a reel has no readable name: ' + repr(rname))
        try:
            clips = [_v(c.name) for c in list(r.clips)]
        except BaseException:
            clips = None
        try:
            sequences = [_v(c.name) for c in list(r.sequences)]
        except BaseException:
            sequences = None
        empty = None
        if clips is not None and sequences is not None:
            empty = len(clips) == 0 and len(sequences) == 0
        rows.append({'name': rname, 'empty': empty, 'clips': clips, 'sequences': sequences})
    return rows


def _reel_is_empty(r):
    return len(list(r.clips)) == 0 and len(list(r.sequences)) == 0


def _tidy_reels_create(batch, recorded, out):
    current = [_v(r.name) for r in list(batch.reels)]
    for row in recorded:
        wanted = row.get('name')
        if row.get('empty') is not True or wanted in current:
            continue
        try:
            made = batch.create_reel(wanted)
            got = _v(made.name) if made is not None else None
        except BaseException as exc:
            out['errors'].append(
                {
                    'step': 'create_reel',
                    'name': wanted,
                    'error': type(exc).__name__ + ': ' + str(exc),
                }
            )
            continue
        if got is None:
            out['errors'].append(
                {'step': 'create_reel', 'name': wanted, 'error': 'create_reel returned no reel'}
            )
            continue
        current.append(got)
        out['created'].append({'wanted': wanted, 'got': got, 'renamed': got != wanted})


def _tidy_reels_delete(batch, recorded, out):
    protected = [row.get('name') for row in recorded] + [c['got'] for c in out['created']]
    reels = list(batch.reels)
    remaining = len(reels)
    for r in reels:
        name = None
        try:
            name = _v(r.name)
            if name in protected:
                continue
            if not _reel_is_empty(r):
                out['kept'].append({'name': name, 'reason': 'holds clips or sequences'})
                continue
            if remaining <= 1:
                out['kept'].append({'name': name, 'reason': 'the last reel of the group'})
                continue
            returned = flame.delete(r, confirm=False)
            out['deleted'].append({'name': name, 'returned': _ja(returned)})
            remaining -= 1
        except BaseException as exc:
            out['errors'].append(
                {'step': 'delete', 'name': name, 'error': type(exc).__name__ + ': ' + str(exc)}
            )


def _tidy_reels(batch, recorded, sections):
    out = {
        'ran': True,
        'created': [],
        'deleted': [],
        'kept': [],
        'errors': [],
        'namesAfter': None,
        'after': None,
    }
    try:
        _tidy_reels_create(batch, recorded, out)
        _tidy_reels_delete(batch, recorded, out)
    except BaseException as exc:
        out['errors'].append({'step': 'tidy', 'error': type(exc).__name__ + ': ' + str(exc)})
    try:
        out['namesAfter'] = [_v(r.name) for r in list(batch.reels)]
        after = _batch_fingerprint(batch, True, sections)
        out['after'] = {
            'fingerprint': after['fingerprint'],
            'sectionData': after.get('sectionData'),
            'unreadableSections': after['unreadableSections'],
        }
    except BaseException as exc:
        out['errors'].append({'step': 'read_back', 'error': type(exc).__name__ + ': ' + str(exc)})
    return out


def _make_private_dirs(directory):
    # Flame runs with umask 0: every directory this creates is chmod'ed to 0700, none that
    # already exists is touched.
    import os
    missing = []
    probe = directory
    while probe and not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for path in reversed(missing):
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)


def _make_private_tree(path):
    # best effort: a file Flame wrote is not worth failing a saved checkpoint over
    import os
    try:
        if os.path.isdir(path):
            os.chmod(path, 0o700)
            for root, dirs, files in os.walk(path):
                for name in dirs:
                    os.chmod(os.path.join(root, name), 0o700)
                for name in files:
                    os.chmod(os.path.join(root, name), 0o600)
        elif os.path.isfile(path):
            os.chmod(path, 0o600)
    except OSError:
        pass


def _save_checkpoint(batch, directory, base, sections=None, full_sections=None):
    import glob
    import os
    if not isinstance(directory, str) or not directory or not isinstance(base, str) or not base:
        raise _OpError('INVALID_ARGS', 'a checkpoint needs a directory and a base name')
    _make_private_dirs(directory)
    pattern = os.path.join(glob.escape(directory), glob.escape(base) + '*')
    before = set(glob.glob(pattern))
    fp = _batch_fingerprint(batch, False, sections)
    reels = None
    reels_error = None
    try:
        reels = _reel_inventory(batch)
    except BaseException as exc:
        reels_error = type(exc).__name__ + ': ' + str(exc)
    full = None
    if full_sections:
        try:
            full = _batch_fingerprint(batch, True, full_sections)
        except BaseException as exc:
            reels_error = (
                (reels_error or '')
                + ' / all-section fingerprint: '
                + type(exc).__name__
                + ': '
                + str(exc)
            )
    returned = batch.save_setup(os.path.join(directory, base))
    written = sorted(set(glob.glob(pattern)) - before)
    for path in written:
        _make_private_tree(path)
    files = [p for p in written if p.endswith('.batch') and os.path.isfile(p)]
    error = None
    if returned is not True:
        error = 'save_setup returned ' + repr(returned) + ' (expected True)'
    elif len(files) != 1:
        error = (
            'save_setup wrote no single .batch file next to '
            + repr(os.path.join(directory, base))
            + '; new paths: '
            + repr(written)
        )
    return {
        'path': files[0] if len(files) == 1 else None,
        'size': os.path.getsize(files[0]) if len(files) == 1 else None,
        'saveSetupReturned': _ja(returned),
        'written': written,
        'batch': _batch_ident(batch),
        'fingerprint': fp['fingerprint'],
        'fingerprintSections': fp['sections'],
        'nodeCount': fp['nodeCount'],
        'connectionCount': fp['connectionCount'],
        'unreadableNodes': fp['unreadableNodes'],
        'reels': reels,
        'reelsError': reels_error,
        'fingerprintFull': full['fingerprint'] if full else None,
        'fingerprintFullSections': list(full_sections) if full and full_sections else None,
        'sectionData': full.get('sectionData') if full else None,
        'error': error,
    }


def _count_nodes(batch):
    try:
        return len(list(batch.nodes))
    except BaseException:
        return None


def _load_setup_checked(batch, path):
    import os
    out = {
        'path': path,
        'loadSetupCalled': False,
        'returned': None,
        'nodeCountBefore': _count_nodes(batch),
        'nodeCountAfter': None,
        'error': None,
        'errorCode': None,
    }
    if not isinstance(path, str) or not os.path.isfile(path):
        out['nodeCountAfter'] = out['nodeCountBefore']
        out['error'] = (
            'no setup file at ' + repr(path) + ': load_setup was NOT called '
            '(a load_setup that fails empties the group)'
        )
        out['errorCode'] = 'PRECONDITION_UNMET'
        return out
    out['loadSetupCalled'] = True
    try:
        out['returned'] = _ja(batch.load_setup(path))
    except BaseException as exc:
        out['nodeCountAfter'] = _count_nodes(batch)
        out['error'] = (
            'load_setup raised ' + type(exc).__name__ + ': ' + str(exc)
            + ' (the group now holds ' + repr(out['nodeCountAfter']) + ' node(s), it held '
            + repr(out['nodeCountBefore']) + ' before)'
        )
        out['errorCode'] = 'FLAME_API_ERROR'
        return out
    out['nodeCountAfter'] = _count_nodes(batch)
    if out['returned'] is not True:
        out['error'] = 'load_setup returned ' + repr(out['returned']) + ' (expected True)'
        out['errorCode'] = 'FLAME_API_ERROR'
    return out


def _eval_json_safe(value):
    import json
    try:
        json.dumps(value)
        return value
    except BaseException:
        return {
            'notJsonSerializable': True,
            'type': type(value).__name__,
            'repr': repr(value)[:2000],
        }
"""
