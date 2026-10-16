"""Prelude source for the `node` and `action` categories' own ops (`ops/node.py`,
`ops/action.py`).

Like `batch_prelude`, `checkpoint_prelude` and `attribute_prelude`: real Python source that
`execution.build_exec_code` places ahead of an op's own code, so each op's `to_python` output is
a few loop-free lines (registry.py's pacing guard parses only that output). It runs inside Flame
(3.11) with `flame`, `_v`, `_j`, `_ja`, `_OpError`, `_try` (batch_prelude), `_attr_equal`
(attribute_prelude) and the selector functions already defined.

What it encodes (measured on Flame 2025.2.4):
  - `PyNode.sockets` is `{'input': {socket: [names of the nodes wired to it]}, 'output': {...}}`;
    every connection check reads it back (a return value alone is not evidence).
  - `PyBatch.connect_nodes(out_node, out_socket, in_node, in_socket)` (socket 'Default' = the
    node's default socket); `PyBatch.disconnect_node(node[, input_socket])` cuts one input
    socket, or every input when the socket is left out.
  - `PyNode.delete(confirm=False)` removes the node from `nodes` and from its neighbours' sockets.
  - `PyNode.duplicate(keep_node_connections=False)` only works while the node's group is the
    current one: `RuntimeError('Nodes need to be in the current batch to be duplicated.')`.
  - `PyNode.save_node_setup(path)` appends an extension that depends on the node's type
    (`.exposure_node`): the real file is found by comparing the directory before and after.
    `PyNode.load_node_setup(path)` of a path that is not there returns True and does nothing.
  - an Action / Image / GMask Tracer node has inner nodes (`nodes`, `node_types`, `media_layers`,
    `output_types`); `create_node(type)` on it can add more than the requested node (an `axis`
    comes with a 3D Path), and `connect_nodes(parent, child, link_type)` links two inner nodes.
  - a write that raises nothing but leaves no trace is an error (`SILENT_NOOP`): every write is
    read back. The op's `_result` is kept beside such an error (execution.py).
"""

from __future__ import annotations

ACTION_LINK_TYPES: tuple[str, ...] = (
    "default",
    "look at",
    "gmask",
    "gmask exclusive",
    "light",
    "light exclusive",
    "mimic",
)
"""`PyActionNode.connect_nodes` link types, from its docstring on Flame 2025.2.4."""

NODE_HELPER = """
_ACTION_FAMILY = ('PyActionNode', 'PyImageNode', 'PyGMaskTracerNode')
_ACTION_LINK_TYPES = ('default', 'look at', 'gmask', 'gmask exclusive', 'light',
                      'light exclusive', 'mimic')


def _node_names(batch):
    return [_v(n.name) for n in list(batch.nodes)]


def _new_names(before, after):
    remaining = list(before)
    added = []
    for n in after:
        if n in remaining:
            remaining.remove(n)
        else:
            added.append(n)
    return added


def _sock_state(node):
    socks = _v(node.sockets)
    if not isinstance(socks, dict):
        raise RuntimeError('node.sockets is a ' + type(socks).__name__ + ', not a dict')
    out = {}
    for side in ('input', 'output'):
        table = socks.get(side)
        if not isinstance(table, dict):
            raise RuntimeError('node.sockets has no ' + side + ' table')
        out[side] = {}
        for sock in table:
            linked = table[sock]
            if not isinstance(linked, (list, tuple)):
                linked = []
            out[side][str(sock)] = sorted(str(c) for c in linked)
    return out


def _resolution_dict(res):
    res = _v(res)
    out = {}
    for key in ('width', 'height', 'bit_depth', 'scan_mode', 'frame_ratio'):
        out[key] = _try(lambda key=key: _ja(getattr(res, key)))
    return out


def _node_keys(nodes):
    return [(_try(lambda n=n: _v(n.name)), _node_pos(n)) for n in nodes]


def _inserted_index(before, after):
    # index of the one entry `after` has and `before` lacks (both are _node_keys lists), or None
    if len(after) != len(before) + 1:
        return None
    i = 0
    while i < len(before) and before[i] == after[i]:
        i += 1
    if after[:i] + after[i + 1:] == before:
        return i
    return None


def _node_index(nodes, node):
    for i, n in enumerate(nodes):
        if n is node:
            return i
    key = (_try(lambda: _v(node.name)), _node_pos(node))
    hits = [i for i, k in enumerate(_node_keys(nodes)) if k == key]
    return hits[0] if len(hits) == 1 else None


def _name_counts(names):
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts


def _node_row(n, counts):
    name = _try(lambda: _v(n.name))
    return {
        'name': name,
        'pyType': type(n).__name__,
        'type': _try(lambda: _ja(n.type)),
        'pos': _node_pos(n),
        'sameNameCount': counts.get(name, 1),
    }


def _ambiguous_socket_names(sockets, counts):
    names = set()
    for side in ('input', 'output'):
        for linked in sockets[side].values():
            names.update(c for c in linked if counts.get(c, 0) > 1)
    return sorted(names)


def _node_info(batch, node, include_values):
    unreadable = []
    attrs = _get_attributes(node, None, include_values)
    ntype = None
    if 'type' in attrs['attributes']:
        ntype = attrs['attributes']['type'].get('value')
    info = {
        'name': _try(lambda: _v(node.name)),
        'pyType': type(node).__name__,
        'type': ntype,
        'attributes': attrs['attributes'],
    }
    info['ambiguousSocketNames'] = None
    try:
        info['sockets'] = _sock_state(node)
        info['ambiguousSocketNames'] = _ambiguous_socket_names(
            info['sockets'], _name_counts(_node_names(batch))
        )
    except BaseException as exc:
        info['sockets'] = None
        unreadable.append('sockets: ' + (str(exc) or type(exc).__name__))
    info['clip'] = None
    if type(node).__name__ == 'PyClipNode':
        clip = _try(lambda: node.clip)
        info['clip'] = {
            'name': _try(lambda: _v(clip.name)),
            'uid': _try(lambda: _v(clip.uid)),
            'pyType': None if clip is None else type(clip).__name__,
            'mediaPath': _try(lambda: _ja(node.media_path)),
            'duration': _try(lambda: _ja(node.duration)),
            'resolution': _try(lambda: _resolution_dict(node.resolution)),
        }
    info['render'] = None
    if type(node).__name__ in ('PyWriteFileNode', 'PyRenderNode'):
        info['render'] = {
            'resolvedMediaPath': _try(lambda: _ja(node.get_resolved_media_path())),
            'rangeStart': _try(lambda: _ja(node.range_start)),
            'rangeEnd': _try(lambda: _ja(node.range_end)),
        }
    info['innerNodeCount'] = None
    if type(node).__name__ in _ACTION_FAMILY:
        info['innerNodeCount'] = _try(lambda: len(list(node.nodes)))
    info['unreadable'] = unreadable
    return info


def _node_types(batch):
    return [str(t) for t in list(batch.node_types)]


def _canonical_type(types, wanted):
    # (canonical spelling or None, every case-insensitive match): an exact spelling wins; else
    # the one type that matches ignoring case (Flame takes 'Mux' for the listed 'MUX')
    if wanted in types:
        return wanted, [wanted]
    low = wanted.lower()
    hits = [t for t in types if t.lower() == low]
    return (hits[0] if len(hits) == 1 else None), hits


def _type_error(what, wanted, hits, count, lister):
    if len(hits) > 1:
        return (
            what + ' ' + repr(wanted) + ' matches ' + str(len(hits)) + ' types ignoring case '
            + repr(hits) + ': give the exact spelling'
        )
    return (
        'unknown ' + what + ' ' + repr(wanted) + ' (' + lister + ' lists the '
        + str(count) + ' types this Flame has)'
    )


def _create_node(batch, node_type, name):
    types = _node_types(batch)
    requested_type = node_type
    node_type, hits = _canonical_type(types, requested_type)
    if node_type is None:
        raise _OpError(
            'INVALID_ARGS',
            _type_error('node type', requested_type, hits, len(types), 'node.list_types'),
        )
    before_nodes = list(batch.nodes)
    before = [_try(lambda n=n: _v(n.name)) for n in before_nodes]
    before_keys = _node_keys(before_nodes)
    if name is not None and name in before:
        raise _OpError(
            'INVALID_ARGS',
            'a node named ' + repr(name) + ' already exists in this Batch group (a non-current '
            'group would silently get two nodes with one name)',
        )
    node = batch.create_node(node_type)
    after_nodes = list(batch.nodes)
    after = [_try(lambda n=n: _v(n.name)) for n in after_nodes]
    inserted = _inserted_index(before_keys, _node_keys(after_nodes))
    added = _new_names(before, after)
    created_name = _try(lambda: _v(node.name))
    found = None
    if created_name is not None and created_name in added:
        found = node
    elif len(added) == 1 and inserted is not None:
        found = after_nodes[inserted]
        created_name = added[0]
    error = None
    error_code = None
    if found is None:
        error = (
            'create_node returned without an error but no new node appeared (had '
            + str(len(before)) + ' node(s), now ' + str(len(after))
            + '; new names ' + repr(added) + ')'
        )
        error_code = 'SILENT_NOOP'
    if found is not None and name is not None:
        try:
            found.name.set_value(name)
        except BaseException as exc:
            error = (
                'the node was created as ' + repr(created_name) + ' but naming it ' + repr(name)
                + ' failed: ' + type(exc).__name__ + ': ' + str(exc)
                + ' (Flame checks a node name against the CURRENT Batch group, not the '
                "node's own group)"
            )
            error_code = 'FLAME_API_ERROR'
    final_names = _node_names(batch)
    final_name = created_name
    if error is None and name is not None:
        final_name = name
        if name not in final_names:
            error = 'the node was renamed to ' + repr(name) + ' but no node of that name is there'
            error_code = 'SILENT_NOOP'
    row = None
    if found is not None:
        counts = _name_counts(final_names)
        final_nodes = list(batch.nodes)
        if inserted is not None and inserted < len(final_nodes):
            row = _try(lambda: _node_row(final_nodes[inserted], counts))
        else:
            row = _try(lambda: _node_row(_resolve_node(batch, final_name), counts))
    return {
        'requestedType': requested_type,
        'canonicalType': node_type,
        'requestedName': name,
        'node': row,
        'createdAs': created_name,
        'typeAsRequested': None if row is None else row['type'] == requested_type,
        'typeAsCanonical': None if row is None else row['type'] == node_type,
        'nodeCountBefore': len(before),
        'nodeCountAfter': len(final_names),
        'addedNames': added,
        'error': error,
        'errorCode': error_code,
    }


def _links_involving(batch, name):
    out = {}
    for n in list(batch.nodes):
        try:
            state = _sock_state(n)
        except BaseException:
            continue
        hits = []
        for side in ('input', 'output'):
            for sock in state[side]:
                if name in state[side][sock]:
                    hits.append(side + ':' + sock)
        if hits:
            out[_v(n.name)] = hits
    return out


def _delete_node(batch, node):
    name = _v(node.name)
    ptype = type(node).__name__
    before = _node_names(batch)
    wired = None
    try:
        wired = _sock_state(node)
    except BaseException:
        wired = None
    returned = node.delete(confirm=False)
    after = _node_names(batch)
    error = None
    if after.count(name) >= before.count(name):
        error = (
            'delete returned ' + repr(_ja(returned)) + ' but a node named ' + repr(name)
            + ' is still there (' + str(after.count(name)) + ' of that name, was '
            + str(before.count(name)) + ')'
        )
    elif returned is not True:
        error = (
            'delete returned ' + repr(_ja(returned)) + ' (expected True), though the node is gone'
        )
    stale = _links_involving(batch, name)
    return {
        'deleted': name,
        'pyType': ptype,
        'nodeCountBefore': len(before),
        'nodeCountAfter': len(after),
        'wasWiredTo': wired,
        'staleLinksInNeighbours': stale,
        'staleLinksAmbiguousNames': [name] if name in after else [],
        'deleteReturned': _ja(returned),
        'error': error,
    }


def _connect_nodes(source_batch, source, target_batch, target, source_socket, target_socket):
    sname = _v(source.name)
    tname = _v(target.name)
    if _v(source_batch.uid) != _v(target_batch.uid):
        raise _OpError(
            'PRECONDITION_UNMET',
            'source ' + repr(sname) + ' and target ' + repr(tname)
            + ' are in different Batch groups',
        )
    if source is target or (
        sname == tname
        and _stable_node(source, source_batch) is not None
        and _stable_node(source, source_batch) == _stable_node(target, target_batch)
    ):
        raise _OpError('INVALID_ARGS', 'cannot connect the node ' + repr(sname) + ' to itself')
    names = _node_names(source_batch)
    src_amb = names.count(sname) > 1
    tgt_amb = names.count(tname) > 1
    s_before = _sock_state(source)
    t_before = _sock_state(target)
    if source_socket != 'Default' and source_socket not in s_before['output']:
        raise _OpError(
            'INVALID_ARGS',
            repr(sname) + ' has no output socket ' + repr(source_socket) + ' (it has '
            + repr(sorted(s_before['output'])) + ')',
        )
    if target_socket != 'Default' and target_socket not in t_before['input']:
        raise _OpError(
            'INVALID_ARGS',
            repr(tname) + ' has no input socket ' + repr(target_socket) + ' (it has '
            + repr(sorted(t_before['input'])) + ')',
        )
    returned = source_batch.connect_nodes(source, source_socket, target, target_socket)
    s_after = _sock_state(source)
    t_after = _sock_state(target)
    linked_in = sorted(s for s in t_after['input'] if sname in t_after['input'][s])
    linked_out = sorted(s for s in s_after['output'] if tname in s_after['output'][s])
    was_in = sorted(s for s in t_before['input'] if sname in t_before['input'][s])
    was_out = sorted(s for s in s_before['output'] if tname in s_before['output'][s])
    if target_socket == 'Default':
        target_end = len(linked_in) > 0
    else:
        target_end = target_socket in linked_in
    if source_socket == 'Default':
        source_end = len(linked_out) > 0
    else:
        source_end = source_socket in linked_out
    # the names a socket lists are all the API gives: a shared name matches the sibling too, so
    # the end whose own partner name is unique is the one that tells this link from the sibling's
    readback_ambiguous = False
    if src_amb and tgt_amb:
        linked = target_end or source_end
        readback_ambiguous = linked
    elif src_amb:
        linked = source_end
    else:
        linked = target_end
    already_linked = len(was_out) > 0 if src_amb and not tgt_amb else len(was_in) > 0
    new_links = [s for s in linked_in if s not in was_in]
    if src_amb and not already_linked:
        new_links = [target_socket] if target_socket != 'Default' and linked else linked_in
    elif src_amb:
        new_links = []
    error = None
    error_code = None
    if not linked:
        where = 'any input socket'
        if target_socket != 'Default':
            where = 'its input socket ' + repr(target_socket)
        error = (
            'connect_nodes returned ' + repr(_ja(returned)) + ' but ' + repr(tname)
            + ' shows no link from ' + repr(sname) + ' on ' + where
            + ' afterwards (input sockets now ' + repr(t_after['input']) + ')'
        )
        error_code = 'SILENT_NOOP'
    elif returned is not True:
        error = (
            'connect_nodes returned ' + repr(_ja(returned))
            + ' (expected True), though the link is there'
        )
        error_code = 'FLAME_API_ERROR'
    replaced = {}
    if target_socket != 'Default':
        now = t_after['input'].get(target_socket, [])
        replaced = {
            target_socket: [c for c in t_before['input'].get(target_socket, []) if c not in now]
        }
    return {
        'source': sname,
        'target': tname,
        'sourceSocket': source_socket,
        'targetSocket': target_socket,
        'linkedInputSockets': linked_in,
        'linkedOutputSockets': linked_out,
        'alreadyLinked': already_linked,
        'newLinks': new_links,
        'readbackAmbiguous': readback_ambiguous,
        'replaced': replaced,
        'targetInputsBefore': t_before['input'],
        'targetInputsAfter': t_after['input'],
        'connectReturned': _ja(returned),
        'error': error,
        'errorCode': error_code,
    }


def _disconnect_node(batch, node, socket):
    name = _v(node.name)
    before = _sock_state(node)
    if socket is not None and socket not in before['input']:
        raise _OpError(
            'INVALID_ARGS',
            repr(name) + ' has no input socket ' + repr(socket) + ' (it has '
            + repr(sorted(before['input'])) + ')',
        )
    if socket is None:
        returned = batch.disconnect_node(node)
    else:
        returned = batch.disconnect_node(node, socket)
    after = _sock_state(node)
    scope = list(before['input']) if socket is None else [socket]
    was = {s: before['input'][s] for s in scope if before['input'][s]}
    remaining = {s: after['input'].get(s) for s in scope if after['input'].get(s)}
    error = None
    error_code = None
    if remaining:
        error = (
            'disconnect_node returned ' + repr(_ja(returned)) + ' but ' + repr(name)
            + ' still has links on ' + repr(remaining)
        )
        error_code = 'SILENT_NOOP'
    elif returned is not True:
        error = (
            'disconnect_node returned ' + repr(_ja(returned))
            + ' (expected True), though nothing is linked'
        )
        error_code = 'FLAME_API_ERROR'
    return {
        'node': name,
        'socket': socket,
        'scope': scope,
        'disconnected': was,
        'hadLinks': len(was) > 0,
        'inputsBefore': before['input'],
        'inputsAfter': after['input'],
        'disconnectReturned': _ja(returned),
        'error': error,
        'errorCode': error_code,
    }


def _duplicate_node(batch, node, keep_connections):
    name = _v(node.name)
    before_nodes = list(batch.nodes)
    before = [_try(lambda n=n: _v(n.name)) for n in before_nodes]
    before_keys = _node_keys(before_nodes)
    try:
        copy = node.duplicate(keep_node_connections=keep_connections)
    except RuntimeError as exc:
        if 'current batch' in str(exc):
            raise _OpError(
                'PRECONDITION_UNMET',
                'Flame refused: ' + str(exc) + ' (a node can only be duplicated while its Batch '
                'group is the current one)',
            )
        raise
    after_nodes = list(batch.nodes)
    after = [_try(lambda n=n: _v(n.name)) for n in after_nodes]
    inserted = _inserted_index(before_keys, _node_keys(after_nodes))
    added = _new_names(before, after)
    copy_name = _try(lambda: _v(copy.name))
    error = None
    if copy_name is None or copy_name not in added:
        if len(added) == 1:
            copy_name = added[0]
        else:
            error = (
                'duplicate returned without an error but the group shows ' + str(len(added))
                + ' new node(s) ' + repr(added) + ', not one'
            )
    row = None
    sockets = None
    if error is None:
        fresh = after_nodes[inserted] if inserted is not None else _resolve_node(batch, copy_name)
        row = _node_row(fresh, _name_counts(after))
        sockets = _try(lambda: _sock_state(fresh))
    return {
        'source': name,
        'keepConnections': keep_connections,
        'node': row,
        'addedNames': added,
        'nodeCountBefore': len(before),
        'nodeCountAfter': len(after),
        'sockets': sockets,
        'error': error,
    }


def _dir_files(directory):
    import os
    out = {}
    for entry in os.scandir(directory):
        try:
            if entry.is_file():
                st = entry.stat()
                out[entry.path] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return out


def _node_save_setup(batch, node, path, overwrite):
    import os
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _OpError('INVALID_ARGS', 'path must be an absolute path, got ' + repr(path))
    directory, name = os.path.split(path)
    if not name:
        raise _OpError('INVALID_ARGS', 'path names a directory, not a file: ' + repr(path))
    if not os.path.isdir(directory):
        raise _OpError('PRECONDITION_UNMET', 'the directory does not exist: ' + repr(directory))
    stem = name.replace('.', '_')
    before = _dir_files(directory)
    existing = sorted(
        p for p in before
        if os.path.basename(p) == name or os.path.basename(p).startswith(name + '.')
        or os.path.basename(p).startswith(stem + '.')
    )
    if existing and not overwrite:
        raise _OpError(
            'PRECONDITION_UNMET',
            'save_setup would write next to ' + repr(existing)
            + '; pass overwrite=true to allow that',
        )
    returned = node.save_node_setup(path)
    after = _dir_files(directory)
    written = sorted(p for p in after if p not in before or after[p] != before[p])
    named = [
        p for p in written
        if os.path.basename(p) in (name, stem)
        or os.path.basename(p).startswith((name + '.', stem + '.'))
    ]
    files = named if named else (written if len(written) == 1 else [])
    node_type = _try(lambda: str(_v(node.type)).lower().replace(' ', '_'))
    own = [
        p for p in files
        if node_type and os.path.basename(p) in (name + '.' + node_type + '_node',
                                                  stem + '.' + node_type + '_node')
    ]
    real = own[0] if len(own) == 1 else (files[0] if len(files) == 1 else None)
    error = None
    if returned is not True:
        error = 'save_node_setup returned ' + repr(_ja(returned)) + ' (expected True)'
    elif not files:
        error = (
            'save_node_setup wrote no file in ' + repr(directory) + '; new or changed: '
            + repr(written)
        )
    elif real is None:
        error = (
            'save_node_setup wrote ' + str(len(files)) + ' files in ' + repr(directory)
            + ' and none of them is the one named after the node type '
            + repr(node_type) + ': ' + repr([os.path.basename(p) for p in files])
        )
    base = None if real is None else os.path.basename(real)
    return {
        'requestedPath': path,
        'path': real,
        'files': files,
        'size': None if real is None else os.path.getsize(real),
        'extension': base[len(name):] if base is not None and base.startswith(name) else None,
        'written': written,
        'overwritten': [p for p in written if p in before],
        'saveSetupReturned': _ja(returned),
        'node': _node_row(node, _name_counts(_node_names(batch))),
        'error': error,
    }


def _snap_value(x):
    v = _v(x)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_snap_value(i) for i in v]
    if type(v).__name__ == 'PyTime':
        return {'timecode': _ja(v.timecode), 'frame': _ja(v.frame)}
    return {'unsnapshotted': type(v).__name__}


def _snap_attrs(obj):
    out = {}
    for key in list(obj.attributes):
        try:
            out[key] = _snap_value(getattr(obj, key))
        except BaseException as exc:
            out[key] = {'unreadable': str(exc) or type(exc).__name__}
    return out


def _attr_diff(before, after):
    out = {}
    for key in sorted(set(before) | set(after)):
        a = before.get(key, {'missing': True})
        b = after.get(key, {'missing': True})
        if not _attr_equal(a, b):
            out[key] = {'before': a, 'after': b}
    return out


def _node_load_setup(batch, node, path):
    import os
    name = _v(node.name)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _OpError('INVALID_ARGS', 'path must be an absolute path, got ' + repr(path))
    if not os.path.isfile(path):
        raise _OpError(
            'PRECONDITION_UNMET',
            'no setup file at ' + repr(path) + ': load_node_setup was NOT called (for a path that '
            'is not there it returns True and does nothing)',
        )
    before = _snap_attrs(node)
    nodes_before = list(batch.nodes)
    index = _node_index(nodes_before, node)
    returned = node.load_node_setup(path)
    error = None
    error_code = None
    diff = None
    try:
        diff = _attr_diff(before, _snap_attrs(node))
    except BaseException as exc:
        nodes_after = list(batch.nodes)
        if index is not None and len(nodes_after) == len(nodes_before):
            try:
                diff = _attr_diff(before, _snap_attrs(nodes_after[index]))
            except BaseException as exc2:
                exc = exc2
        if diff is None:
            error = (
                'load_node_setup returned ' + repr(_ja(returned)) + ' but the node ' + repr(name)
                + ' cannot be read afterwards (' + (str(exc) or type(exc).__name__)
                + '): it may have been replaced by the setup'
            )
            error_code = 'FLAME_API_ERROR'
    if error is None and returned is not True:
        error = 'load_node_setup returned ' + repr(_ja(returned)) + ' (expected True)'
        error_code = 'FLAME_API_ERROR'
    return {
        'path': path,
        'node': name,
        'loadReturned': _ja(returned),
        'noChange': None if diff is None else len(diff) == 0,
        'changed': diff,
        'changedKeys': None if diff is None else sorted(diff),
        'error': error,
        'errorCode': error_code,
    }
"""

ACTION_HELPER = """
def _action_family(node):
    if type(node).__name__ not in _ACTION_FAMILY:
        raise _OpError(
            'SELECTOR_TYPE',
            'node ' + repr(_try(lambda: _v(node.name))) + ' is a ' + type(node).__name__
            + ', not an Action / Image / GMask Tracer node',
        )


def _plain(x):
    v = _v(x)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return {'pyType': type(v).__name__, 'name': _try(lambda: _v(v.name))}


def _inner_row(c):
    return {
        'name': _try(lambda: _v(c.name)),
        'pyType': type(c).__name__,
        'type': _try(lambda: _ja(c.type)),
    }


def _inner_rows(node):
    return [_inner_row(c) for c in list(node.nodes)]


def _action_list(batch, node):
    _action_family(node)
    unreadable = []

    def field(name, fn):
        try:
            return fn()
        except BaseException as exc:
            unreadable.append(name + ': ' + (str(exc) or type(exc).__name__))
            return None

    return {
        'node': _node_row(node, _name_counts(_node_names(batch))),
        'nodes': field('nodes', lambda: _inner_rows(node)),
        'nodeTypes': field('node_types', lambda: [str(t) for t in list(node.node_types)]),
        'mediaLayers': field('media_layers', lambda: [_plain(x) for x in list(node.media_layers)]),
        'outputTypes': field('output_types', lambda: [str(t) for t in list(node.output_types)]),
        'unreadable': unreadable,
    }


def _action_create_node(node, inner_type):
    _action_family(node)
    types = [str(t) for t in list(node.node_types)]
    requested_type = inner_type
    inner_type, hits = _canonical_type(types, requested_type)
    if inner_type is None:
        raise _OpError(
            'INVALID_ARGS',
            _type_error(
                'inner node type', requested_type, hits, len(types), 'action.list_nodes'
            ),
        )
    before = _inner_rows(node)
    before_names = [r['name'] for r in before]
    created = node.create_node(inner_type)
    after = _inner_rows(node)
    after_names = [r['name'] for r in after]
    added_names = _new_names(before_names, after_names)
    created_name = _try(lambda: _v(created.name))
    error = None
    if not added_names:
        error = (
            'create_node returned without an error but the Action gained no inner node (had '
            + repr(before_names) + ', now ' + repr(after_names) + ')'
        )
    elif created_name is None or created_name not in added_names:
        error = (
            'create_node returned ' + repr(created_name)
            + ', which is not among the new inner nodes ' + repr(added_names)
        )
    return {
        'requestedType': requested_type,
        'canonicalType': inner_type,
        'created': _try(lambda: _inner_row(created)),
        'added': [r for r in after if r['name'] in added_names],
        'alsoAdded': [n for n in added_names if n != created_name],
        'nodesBefore': before_names,
        'nodesAfter': after_names,
        'error': error,
    }


def _co_read(node, method, args):
    # one raw attempt to read a link list; the exception, if any, is kept as text
    label = method + '(' + ', '.join(repr(a) for a in args) + ')'
    try:
        member = getattr(node, method)
        if member is None:
            raise AttributeError(method + ' is not defined on ' + type(node).__name__)
        value = member(*args) if callable(member) else member
        names = sorted(str(_v(x.name)) for x in list(value))
        return {'call': label, 'ok': True, 'names': names, 'error': None}
    except BaseException as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        return {'call': label, 'ok': False, 'names': None, 'error': error}


def _co_state(parent, child, link_type):
    # the calls with the link type given, then the calls with no argument
    return {
        'childParents': [
            _co_read(child, 'parents', (link_type,)),
            _co_read(child, 'parents', ()),
        ],
        'parentChildren': [
            _co_read(parent, 'children', (link_type,)),
            _co_read(parent, 'children', ()),
        ],
    }


def _co_shows(state, pname, cname):
    # (calls that could be read, calls that show the link)
    readable = []
    showing = []
    for row in state['childParents']:
        if row['ok']:
            readable.append(row['call'])
            if pname in row['names']:
                showing.append('child.' + row['call'])
    for row in state['parentChildren']:
        if row['ok']:
            readable.append(row['call'])
            if cname in row['names']:
                showing.append('parent.' + row['call'])
    return readable, showing


def _action_connect(parent_batch, action, parent, child_batch, child_action, child, link_type):
    if _v(parent_batch.uid) != _v(child_batch.uid) or _v(action.name) != _v(child_action.name):
        raise _OpError(
            'PRECONDITION_UNMET',
            'parent and child are not inner nodes of the same Action node',
        )
    _action_family(action)
    if link_type.lower() not in _ACTION_LINK_TYPES:
        raise _OpError(
            'INVALID_ARGS',
            'link_type ' + repr(link_type) + ' is not one of ' + repr(list(_ACTION_LINK_TYPES)),
        )
    pname = _v(parent.name)
    cname = _v(child.name)
    if pname == cname:
        raise _OpError(
            'INVALID_ARGS', 'cannot connect the inner node ' + repr(pname) + ' to itself'
        )
    before = _co_state(parent, child, link_type)
    returned = action.connect_nodes(parent, child, link_type)
    after = _co_state(parent, child, link_type)
    readable, showing = _co_shows(after, pname, cname)
    _seen_before, was_showing = _co_shows(before, pname, cname)
    linked = None if not readable else len(showing) > 0
    error = None
    error_code = None
    if linked is False:
        error = (
            'connect_nodes returned ' + repr(_ja(returned)) + ' but none of the '
            + str(len(readable)) + ' link reads that worked shows the link ' + repr(pname)
            + ' -> ' + repr(cname) + ' (' + repr(after) + ')'
        )
        error_code = 'SILENT_NOOP'
    elif returned is not True:
        error = 'connect_nodes returned ' + repr(_ja(returned)) + ' (expected True)'
        error_code = 'FLAME_API_ERROR'
    return {
        'action': _v(action.name),
        'parent': pname,
        'child': cname,
        'linkType': link_type,
        'linked': linked,
        'readBackAvailable': linked is not None,
        'linkedBy': showing,
        'alreadyLinked': len(was_showing) > 0,
        'before': before,
        'after': after,
        'connectReturned': _ja(returned),
        'error': error,
        'errorCode': error_code,
    }
"""
