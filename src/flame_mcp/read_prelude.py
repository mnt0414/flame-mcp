"""Prelude source for the read ops: `segment.list_effects`, `segment.shared_sources`,
`desktop.find`, `desktop.info` (`ops/segment.py`, `ops/desktop.py`).

Like `attribute_prelude`: real Python source that `execution.build_exec_code` places ahead of an
op's own code, so each op's `to_python` output stays a few lines. It runs inside Flame (3.11)
with `flame`, `_v`, `_j`, `_ja`, `_describe_entry`, `_wiretap_selector`, `_segment_list` and
`_OpError` already defined.

What it encodes (measured on Flame 2025.2.4):
  - `PySegment.effects` is a list of PyTimelineFX; `fx.type` is a plain str;
    `list(fx.attributes)` is `['bypass']` for most types (a few more for Burn-in Letterbox),
    each read one by one.
  - `PySegment` / `PyTrack` / `PyVersion` have no uid and no wiretap id, and a wrapper is a fresh
    object on every access, so a segment is never identified by `==` / `is`. A segment returned by
    `shared_source_segments()` is placed by walking its parent chain (PyTrack -> PyVersion ->
    PyClip / PySequence) to the container and then counting the container's segments that have the
    same (record_in.frame, record_out.frame, source_name, source_in.frame): exactly one is a
    position that can be addressed; several are reported as ambiguous, with candidates.
  - `shared_source_segments()`: from a segment on the Desktop it answers the
    Desktop's segments of the same source, the segment itself included (another sequence, copies
    on the Desktop, the segment inside a reel clip -- a PyVersion's parent is the PySequence or the
    PyClip); a copy made in a library is not answered, and from a segment inside a library it
    answers that segment only.
  - `PyTime.frame` is the absolute frame (86400 for the first segment of a sequence that starts at
    01:00:00:00).
  - `flame.find_by_name(name)` answers a list from the Desktop and the libraries together, `[]` for
    a name nothing carries, and takes `parent=` to narrow it; `flame.find_by_uid(uid)` answers one
    object or None.
"""

from __future__ import annotations

READ_HELPER = """
_RP_MEDIA_TYPES = ('PyClip', 'PySequence')
_RP_CONTAINER_TYPES = ('PyReel', 'PyReelGroup', 'PyFolder', 'PyLibrary', 'PyDesktop')
_RP_MAX_CANDIDATES = 5


def _rp_try(fn):
    try:
        return fn()
    except BaseException as exc:
        return {'error': str(exc) or type(exc).__name__}


def _rp_ref(obj):
    if obj is None:
        return None
    try:
        name = _v(obj.name)
    except BaseException:
        name = None
    return {'type': type(obj).__name__, 'name': name}


def _rp_parent_ref(obj):
    try:
        return _rp_ref(obj.parent)
    except BaseException:
        return None


def _rp_effect_types(seg):
    try:
        return [_j(fx.type) for fx in list(seg.effects)]
    except BaseException:
        return []


def _rp_segment_facts(seg):
    kind = _j(seg.type)
    return {
        'type': kind,
        'isGap': kind == 'Gap',
        'head': _j(seg.head),
        'tail': _j(seg.tail),
        'effects': _rp_effect_types(seg),
    }


def _rp_segment_source(seg):
    # Where the segment's media comes from. A Gap has no source, and a name the segment does
    # not carry or cannot read is None (the catch-all of a Flame object answers None).
    out = {'filePath': None, 'tapeName': None, 'sourceUid': None, 'sourceUnlinked': None}
    if _j(seg.type) == 'Gap':
        return out
    for key, name in (
        ('filePath', 'file_path'),
        ('tapeName', 'tape_name'),
        ('sourceUid', 'source_uid'),
        ('sourceUnlinked', 'source_unlinked'),
    ):
        try:
            out[key] = _j(getattr(seg, name, None))
        except BaseException:
            out[key] = None
    return out


def _rp_effects(seg, include_available):
    effects = []
    for i, fx in enumerate(list(seg.effects)):
        try:
            names = list(fx.attributes)
        except BaseException:
            names = []
        attrs = {}
        for key in names:
            attrs[key] = _rp_try(lambda key=key, fx=fx: _ja(getattr(fx, key)))
        effects.append({'index': i, 'type': _j(fx.type), 'attributes': attrs})
    out = {'effects': effects}
    if include_available:
        out['availableTypes'] = [_j(t) for t in list(seg.effect_types)]
    return out


def _rp_key(seg):
    return (seg.record_in.frame, seg.record_out.frame, _v(seg.source_name), seg.source_in.frame)


def _rp_positions(container):
    found = {}
    for vi, ver in enumerate(list(container.versions)):
        for ti, trk in enumerate(list(ver.tracks)):
            for si, seg in enumerate(list(trk.segments)):
                found.setdefault(_rp_key(seg), []).append((vi, ti, si))
    return found


def _rp_position(parent, version, track, seg):
    segments = _segment_list(parent, version, track)
    frame = seg.record_in.frame
    hits = [i for i, s in enumerate(segments) if s.record_in.frame == frame]
    if len(hits) != 1:
        return None
    return (version, track, hits[0])


def _rp_shared_item(seg, target_id, target_pos, cache):
    item = {
        'container': None,
        'recordIn': _rp_try(lambda: _ja(seg.record_in)),
        'recordOut': _rp_try(lambda: _ja(seg.record_out)),
        'sourceIn': _rp_try(lambda: _ja(seg.source_in)),
        'sourceOut': _rp_try(lambda: _ja(seg.source_out)),
        'sourceName': _rp_try(lambda: _v(seg.source_name)),
        'isTarget': False,
        'selector': None,
        'ambiguous': False,
    }
    try:
        container = seg.parent.parent.parent
        csel = _wiretap_selector(container)
        item['container'] = {
            'type': type(container).__name__,
            'name': _rp_ref(container)['name'],
            'selector': csel,
            'parent': _rp_parent_ref(container),
        }
        wid = None if csel is None else csel['id']
        if wid is not None and wid in cache:
            positions = cache[wid]
        else:
            positions = _rp_positions(container)
            if wid is not None:
                cache[wid] = positions
        found = positions.get(_rp_key(seg), [])
    except BaseException as exc:
        item['error'] = str(exc) or type(exc).__name__
        return item
    if len(found) > 1:
        item['ambiguous'] = True
        item['candidates'] = [
            {'version': v, 'track': t, 'index': i} for v, t, i in found[:_RP_MAX_CANDIDATES]
        ]
    elif len(found) == 1 and csel is not None:
        v, t, i = found[0]
        item['selector'] = {
            'kind': 'segment',
            'parent': csel,
            'version': v,
            'track': t,
            'index': i,
        }
        item['isTarget'] = wid == target_id and target_pos == (v, t, i)
    return item


def _rp_find(name, uid, parent):
    if uid is not None:
        found = flame.find_by_uid(uid)
        return [] if found is None else [found]
    if parent is None:
        return list(flame.find_by_name(name))
    return list(flame.find_by_name(name, parent=parent))


def _rp_find_item(obj):
    entry = _describe_entry(obj)
    wiretap_id = entry['wiretapId']
    selector = None if wiretap_id is None else {'kind': 'wiretap', 'id': wiretap_id}
    return {
        'type': entry['type'],
        'name': entry['name'],
        'uid': entry['uid'],
        'selector': selector,
        'parent': _rp_parent_ref(obj),
    }


def _rp_child_counts(obj):
    counts = {}
    for child in list(obj.children):
        kind = type(child).__name__
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _rp_media_basics(obj):
    return {
        'startFrame': _rp_try(lambda: _ja(obj.start_frame)),
        'startTime': _rp_try(lambda: _ja(obj.start_time)),
        'width': _rp_try(lambda: _ja(obj.width)),
        'height': _rp_try(lambda: _ja(obj.height)),
        'audioTracks': _rp_try(lambda: len(list(obj.audio_tracks))),
    }


def _rp_media_info(obj):
    return {
        **_rp_media_basics(obj),
        'duration': _rp_try(lambda: _ja(obj.duration)),
        'frameRate': _rp_try(lambda: _ja(obj.frame_rate)),
        'ratio': _rp_try(lambda: _ja(obj.ratio)),
        'bitDepth': _rp_try(lambda: _ja(obj.bit_depth)),
        'scanMode': _rp_try(lambda: _ja(obj.scan_mode)),
        'cached': _rp_try(lambda: _ja(obj.cached)),
        'unlinked': _rp_try(lambda: _ja(obj.unlinked)),
        'versions': _rp_try(lambda: [len(list(ver.tracks)) for ver in list(obj.versions)]),
    }


def _rp_info(obj):
    entry = _describe_entry(obj)
    out = {
        'type': entry['type'],
        'name': entry['name'],
        'uid': entry['uid'],
        'wiretapId': entry['wiretapId'],
        'parent': _rp_parent_ref(obj),
    }
    if entry['type'] in _RP_MEDIA_TYPES:
        out.update(_rp_media_info(obj))
    elif entry['type'] in _RP_CONTAINER_TYPES:
        out['childCounts'] = _rp_try(lambda: _rp_child_counts(obj))
    return out
"""
