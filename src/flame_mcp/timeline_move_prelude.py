"""Prelude source for `timeline.move_track` (`ops/timeline_build.py`).

Like `timeline_build_prelude`: real Python source that `execution.build_exec_code` places ahead of
an op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`,
`_segment_list` and the `_tl_*` / `_tb_*` / `_dm_*` / `_dc_*` / `_ex_*` / `_td_*` helpers already
defined.

What it encodes (measured on Flame 2025.2.4):
  - nothing moves a track: PySequence, PyVersion, PyTrack and PyAudioTrack expose no move or order
    method (PyTrack's attributes are name, hidden, locked, stereo_linked, selected_segments,
    selected_transitions, selection). So the track is rebuilt, on a copy of a 4-track sequence with
    Timewarp / Resize / Action segments, an internal Gap and an Unlinked Video segment:
      1. `clip = track.copy_to_media_panel(reel)` makes a PySequence clip of the track's segments
         from its FIRST media segment to its LAST (internal Gaps kept; the leading and the
         trailing Gap are not in it). The reel is the stash reel of the Timeline checkpoints.
      2. `version.create_track(track_index=k)`: k is 1-BASED. k=1 inserts at the bottom (list
         index 0), k=0 raises RuntimeError 'Invalid track index.', -1 appends at the top. The new
         track is one Gap as long as the sequence and the tracks at list index >= k - 1 move up
         by one.
      3. `seq.overwrite(clip, flame.PyTime(first_media_record_in, seq.frame_rate), new)` answers
         True and the new track equals the source row for row (type, record_in / record_out,
         source_in / source_out, source_name, segment name, effect types); Action and Timewarp
         setups saved from both are identical except Timewarp Current_Time and the evaluated
         Value of the TW_Timing channel (display state); connected_segments() of the new segments
         is as many as the source's. Overwriting at frame 0 shifts everything, so the first media
         record_in is used.
      4. `flame.delete(source_track, confirm=False)` answers True and removes the track; the other
         tracks are unchanged.
      5. the by-product clip is deleted from the stash reel (only that object: the one new entry
         of the reel, the same care as `timeline.delete_segments`, `_td_cleanup`).
  - PyTrack.transitions lists a PyTransition at EVERY segment boundary: type 'Cut', duration 0,
    in_offset 0, alignment 'Custom' (measured: a track of 13 segments lists 12, one of
    4 lists 3). The recipe was measured on tracks that hold only those, so they are fine; any
    other transition (a dissolve ...) or one whose type or duration cannot be read is refused up
    front (PRECONDITION_UNMET), as are a stereo_linked track and a version with a single track:
    moving them was NOT measured.
  - the Gap(s) at the end of a track (all of a Gap-only track) are left out of the comparison and
    of the copy, so an effect on one would be lost unseen. Whether a Gap takes an effect was NOT
    measured: a track whose such Gap has an effect, or whose effects cannot be read, is refused up
    front too (PRECONDITION_UNMET). The leading and the internal Gaps are in the comparison.
  - a source track that is only Gap(s): steps 1, 3 and 5 are skipped, the empty track is created
    at the target and the source is deleted. Measured (top to bottom of a real copy):
    the order was [3, 0, 1, 2], name / hidden / locked were copied to the new track and the audio
    rows (type, record and source frames, source_name) read back complete.
  - the audio of a sequence with sound (video track 0 holding `p2_av72`, which brought two audio
    channels in, and a second video track; measured on Flame 2025.2.4 and 2026.2.3): moving track 1,
    which holds no audio clip, to the bottom (to=0), and moving track 0, which holds the audio
    clip, to the top (to=1), both left the audio rows and `result.audio` (changed=false,
    complete=true) as they were and gave the right order; `journal.revert` of each put the
    fingerprint, the video rows and the audio rows back. The audio did not change in either, so
    this says nothing about a revert of an audio change.
  - the result is judged by reading every track back (`_tm_sig`): the order must be the old order
    with the source moved to `to`. A track is compared by its segments without the Gap(s) at its
    end (a new track is one Gap as long as the sequence, a source shorter than the sequence has no
    trailing Gap): that tolerance is this code's own choice, not a measurement. The copy is also
    checked against the source BEFORE the source is deleted; a copy that does not match leaves the
    source where it is.
  - `k` for the position `to` in the FINAL order: to < track gives to + 1 (the source moves up by
    one), to > track gives to + 2 (the new track goes above the target while the source, still
    below it, is deleted). Moving to the very top would be k = count + 1, which was not measured as
    a valid index, so -1 (measured: append at the top) is sent instead.

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

MOVE_TRACK_HELPER = """
_TM_ATTRS = ('name', 'hidden', 'locked')
_TM_FIRST_SOURCES = 3
_TM_PROBLEMS = 6


def _tm_effect_types(seg):
    return [_j(fx.type) for fx in list(seg.effects)]


def _tm_sig(seq, version, track):
    # what makes two tracks the same track: type, record and source range, source name, segment
    # name and the effect types of every segment. The Gap(s) at the END of a track are left out:
    # a track made by create_track is one Gap as long as the sequence, so it ends with a Gap that
    # a source track shorter than the sequence does not have.
    segs = _segment_list(seq, version, track)
    while segs and _j(segs[-1].type) == 'Gap':
        segs = segs[:-1]
    rows = []
    for seg in segs:
        rows.append([
            _j(seg.type),
            seg.record_in.frame,
            seg.record_out.frame,
            seg.source_in.frame,
            seg.source_out.frame,
            _v(seg.source_name),
            _try(lambda seg=seg: _v(seg.name)),
            _try(lambda seg=seg: _tm_effect_types(seg)),
        ])
    return rows


def _tm_view(seq, version):
    # the tracks of a version in order: how many segments, how many of them media, the first
    # source names
    view = []
    for ti in range(len(list(list(seq.versions)[version].tracks))):
        segs = _segment_list(seq, version, ti)
        media = [s for s in segs if _j(s.type) != 'Gap']
        view.append({
            'track': ti,
            'segmentCount': len(segs),
            'mediaCount': len(media),
            'firstSources': [_v(s.source_name) for s in media[:_TM_FIRST_SOURCES]],
        })
    return view


def _tm_k(track, to, count):
    # the 1-based index for create_track that leaves the new track at list index `to` once the
    # source (at `track`) is deleted; -1 appends at the top (to the last place)
    if to < track:
        return to + 1
    if to == count - 1:
        return -1
    return to + 2


def _tm_transition(tr):
    # (type, duration in frames) of a PyTransition; type and duration may be PyAttributes or plain
    kind = _v(tr.type)
    duration = _v(tr.duration)
    if type(duration).__name__ == 'PyTime':
        duration = duration.frame
    return kind, duration


def _tm_refusal(src):
    # why the track cannot be moved (it was not measured), or None. PyTrack.transitions lists a
    # 'Cut' transition of duration 0 at every segment boundary (measured); a track that
    # holds only those is the kind the recipe was measured on. Any other transition, or one whose
    # type or duration cannot be read, refuses.
    try:
        transitions = list(src.transitions)
    except BaseException as exc:
        return (
            'PyTrack.transitions cannot be read (' + type(exc).__name__ + ': ' + str(exc)
            + '), so it is not known whether the track has a transition other than a Cut, and '
            'such a track is refused: nothing was done'
        )
    other = []
    for index, tr in enumerate(transitions):
        try:
            kind, duration = _tm_transition(tr)
        except BaseException as exc:
            return (
                'transition ' + str(index) + ' of the track cannot be read (type, duration: '
                + type(exc).__name__ + ': ' + str(exc) + '), so it is not known whether it is a '
                'Cut of duration 0, and a track with another transition is refused: '
                'nothing was done'
            )
        if kind != 'Cut' or duration != 0:
            other.append([index, kind, duration])
    if other:
        return (
            'the track has ' + str(len(other)) + ' transition(s) that are not a Cut of duration 0 '
            '(first: index ' + str(other[0][0]) + ', type ' + repr(other[0][1]) + ', duration '
            + repr(other[0][2]) + '), and such a track is refused: nothing was done'
        )
    try:
        linked = _v(src.stereo_linked)
    except BaseException as exc:
        return (
            'PyTrack.stereo_linked cannot be read (' + type(exc).__name__ + ': ' + str(exc)
            + '), and a stereo_linked track is refused: nothing was done'
        )
    if linked:
        return (
            'the track is stereo_linked, which is refused: nothing was done'
        )
    return None


def _tm_gap_fx_refusal(seq, version, track):
    # why the track cannot be moved because of its trailing Gap(s), or None. _tm_sig leaves the
    # Gap(s) at the end of a track out of the comparison (all of a Gap-only track), so an effect on
    # one of them would be lost without the readback noticing. Whether a Gap takes an effect was
    # not measured: one that has an effect, or whose effects cannot be read, refuses.
    segs = _segment_list(seq, version, track)
    end = len(segs)
    while end > 0 and _j(segs[end - 1].type) == 'Gap':
        end -= 1
    carrying = []
    for index in range(end, len(segs)):
        try:
            count = len(list(segs[index].effects))
        except BaseException as exc:
            return (
                'the effects of the Gap at index ' + str(index) + ' (one that the comparison '
                'leaves out: a Gap at the end of the track) cannot be read (' + type(exc).__name__
                + ': ' + str(exc) + '), so it is not known whether it carries an effect, and '
                'a Gap with an effect is refused: nothing was done'
            )
        if count:
            carrying.append([index, segs[index].record_in.frame, count])
    if carrying:
        return (
            str(len(carrying)) + ' Gap(s) that the comparison leaves out (the Gap(s) at the end of '
            'the track, or all of a track that is only Gap(s)) carry an effect (first: index '
            + str(carrying[0][0]) + ', record_in ' + str(carrying[0][1]) + ', '
            + str(carrying[0][2]) + ' effect(s)); a track with such a Gap is refused: nothing was '
            'done'
        )
    return None


def _tm_check(seq, version, track, to):
    out = _tb_new({'version': version, 'track': track, 'to': to, 'moved': None})
    if _tb_start(out, seq, 'move_track needs a sequence')['error'] is not None:
        return out
    versions = list(seq.versions)
    if version >= len(versions):
        raise _OpError(
            'SELECTOR_EMPTY',
            'version index ' + str(version) + ' out of range (have ' + str(len(versions)) + ')',
        )
    tracks = list(versions[version].tracks)
    count = len(tracks)
    if track >= count:
        raise _OpError(
            'SELECTOR_EMPTY',
            'track index ' + str(track) + ' out of range (have ' + str(count) + ')',
        )
    if count < 2:
        return _tb_fail(
            out,
            'PRECONDITION_UNMET',
            'the version has a single track: there is no other place to move it to',
        )
    if to >= count:
        raise _OpError(
            'SELECTOR_EMPTY',
            'to ' + str(to) + ' is out of range (the version has ' + str(count) + ' tracks)',
        )
    if to == track:
        return _tb_fail(out, 'INVALID_ARGS', 'to is the track itself: it would not move')
    src = tracks[track]
    refusal = _tm_refusal(src)
    if refusal is not None:
        return _tb_fail(out, 'PRECONDITION_UNMET', refusal)
    refusal = _tm_gap_fx_refusal(seq, version, track)
    if refusal is not None:
        return _tb_fail(out, 'PRECONDITION_UNMET', refusal)
    for owner, name in ((src, 'copy_to_media_panel'), (versions[version], 'create_track')):
        if not callable(getattr(owner, name, None)):
            return _tb_fail(
                out,
                'PRECONDITION_UNMET',
                'this Flame has no ' + type(owner).__name__ + '.' + name + ': nothing was done',
            )
    out['moved'] = {'from': track, 'to': to, 'k': _tm_k(track, to, count)}
    out['before'] = {'tracks': _tm_view(seq, version)}
    return out


def _tm_copy_attrs(wanted, new_track):
    # name / hidden / locked of the source onto the new track, each read back; never raises
    out = {'copied': [], 'failed': []}
    for key in _TM_ATTRS:
        want = wanted.get(key)
        got = None
        error = None
        try:
            attr = getattr(new_track, key)
            if type(attr).__name__ == 'PyAttribute':
                attr.set_value(want)
            else:
                setattr(new_track, key, want)
            got = _ja(getattr(new_track, key))
        except BaseException as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        if error is None and got == want:
            out['copied'].append(key)
        else:
            out['failed'].append({'key': key, 'wanted': want, 'got': got, 'error': error})
    return out


def _tm_problems(wanted_sigs, have_sigs):
    problems = []
    if len(wanted_sigs) != len(have_sigs):
        problems.append(
            'the version has ' + str(len(have_sigs)) + ' track(s), expected '
            + str(len(wanted_sigs))
        )
        return problems
    for i, (want, have) in enumerate(zip(wanted_sigs, have_sigs)):
        if want != have and len(problems) < _TM_PROBLEMS:
            problems.append(
                'track ' + str(i) + ' holds ' + str(len(have)) + ' segment(s) '
                + repr(have[:2]) + ', expected ' + str(len(want)) + ' ' + repr(want[:2])
            )
    return problems


def _tm_remove_new_track(seq, version, new_at, sigs_before):
    # takes the track create_track added away again (flame.delete of a track is measured); only
    # when the version has one track more than before and every other track is as it was
    out = {'attempted': False, 'deleted': False, 'returned': None, 'error': None}
    count = len(sigs_before)
    try:
        now = list(list(seq.versions)[version].tracks)
        sigs_now = [_tm_sig(seq, version, i) for i in range(len(now))]
        if len(now) != count + 1 or sigs_now[:new_at] + sigs_now[new_at + 1:] != sigs_before:
            out['error'] = (
                'the version has ' + str(len(now)) + ' track(s) and the others are not as they '
                'were: nothing was removed'
            )
            return out
        out['attempted'] = True
        out['returned'] = _ja(flame.delete(now[new_at], confirm=False))
        out['deleted'] = len(list(list(seq.versions)[version].tracks)) == count
        if not out['deleted']:
            out['error'] = (
                'flame.delete returned ' + repr(out['returned']) + ' and the track is still there'
            )
    except BaseException as exc:
        out['error'] = type(exc).__name__ + ': ' + str(exc)
    return out


def _tm_move(seq, version, track, to, library_name, reel_name):
    out = _tm_check(seq, version, track, to)
    if out['error'] is not None:
        return out
    ver = list(seq.versions)[version]
    count = len(list(ver.tracks))
    k = out['moved']['k']
    src = list(ver.tracks)[track]
    sigs_before = [_tm_sig(seq, version, i) for i in range(count)]
    media = [s for s in _segment_list(seq, version, track) if _j(s.type) != 'Gap']
    first = media[0].record_in.frame if media else None
    wanted_attrs = {key: _try(lambda key=key: _ja(getattr(src, key))) for key in _TM_ATTRS}
    fp_before = _tl_fingerprint(seq)['fingerprint']
    audio_before = _tb_audio_read(seq)
    new_at = count if k == -1 else k - 1
    src_at = track + 1 if new_at <= track else track
    out.update(
        verification=None, clip=None, cleanup=None, attributes=None, audio=None, display=None,
        stash=None, libraryCreated=None, reelCreated=None, after=None, changed=None,
        returned={'copy': None, 'overwrite': None, 'delete': None}, exception=None,
        stage='start', notices=[], warnings=[], ran=True,
    )
    shown_before = _tl_shown()
    reel = None
    dest_entry = None
    before_uids = []
    if media:
        lib, reel, out['libraryCreated'], out['reelCreated'] = _tl_stash_reel(
            library_name, reel_name
        )
        out['stash'] = {'library': _try(lambda: _v(lib.name)), 'reel': _try(lambda: _v(reel.name))}
        dest_entry = _dc_with_selector(reel)
        before_uids = [_dm_uid(o) for o in _dm_media_children(reel)]
    else:
        out['notices'].append(
            'the track holds only Gap(s): no copy was made, the empty track create_track makes is '
            'put at the target and the source is deleted (measured on Flame 2025.2.4)'
        )
    attempted = False
    made = None
    new = []
    got = []
    problem = None
    created = False
    deleted = False
    try:
        if media:
            out['stage'] = 'copy_to_media_panel'
            attempted = True
            returned = src.copy_to_media_panel(reel)
            got, shape = _ex_answer(returned)
            out['returned']['copy'] = {'shape': shape, 'types': [type(o).__name__ for o in got]}
            new = _dm_new_entries(before_uids, _dm_media_children(reel))
            made = _tl_pick_new(new, got) if new else None
            if made is None:
                problem = (
                    'copy_to_media_panel made no entry of the stash reel that can be told from '
                    'the others (' + str(len(new)) + ' new): the track was not rebuilt'
                )
            else:
                out['clip'] = {
                    'type': type(made).__name__,
                    'name': _try(lambda: _v(made.name)),
                    'uid': _dm_uid(made),
                    'duration': _try(lambda: _j(made.duration)),
                }
        if problem is None:
            out['stage'] = 'create_track'
            ver.create_track(track_index=k)
            now = list(list(seq.versions)[version].tracks)
            if len(now) != count + 1:
                problem = (
                    'create_track(track_index=' + str(k) + ') left ' + str(len(now))
                    + ' tracks, expected ' + str(count + 1)
                )
            else:
                sigs_now = [_tm_sig(seq, version, i) for i in range(count + 1)]
                others = sigs_now[:new_at] + sigs_now[new_at + 1:]
                if others != sigs_before or sigs_now[new_at]:
                    problem = (
                        'create_track(track_index=' + str(k) + ') did not add an empty track at '
                        'list index ' + str(new_at) + ' and leave the others as they were'
                    )
                else:
                    created = True
        if problem is None and media:
            out['stage'] = 'overwrite'
            new_track = list(list(seq.versions)[version].tracks)[new_at]
            returned = seq.overwrite(made, _tb_time(seq, first), new_track)
            out['returned']['overwrite'] = _ja(returned)
            have = _tm_sig(seq, version, new_at)
            if have != sigs_before[track]:
                problem = (
                    'the new track does not equal the source track after overwrite (returned '
                    + repr(out['returned']['overwrite']) + '): '
                    + '; '.join(_tm_problems([sigs_before[track]], [have]))
                )
        if problem is None:
            out['stage'] = 'audio'
            if _tb_audio_read(seq)['rows'] != audio_before['rows']:
                problem = (
                    'the audio tracks changed after the overwrite (result.audio): the source '
                    'track was kept'
                )
        if problem is None:
            out['stage'] = 'attributes'
            new_track = list(list(seq.versions)[version].tracks)[new_at]
            out['attributes'] = _tm_copy_attrs(wanted_attrs, new_track)
            for item in out['attributes']['failed']:
                out['warnings'].append(
                    'track ' + item['key'] + ': ' + repr(item['wanted']) + ' was put on the new '
                    'track and ' + repr(item['got']) + ' was read back'
                    + ('' if item['error'] is None else ' (' + item['error'] + ')')
                )
            out['stage'] = 'delete'
            victim = list(list(seq.versions)[version].tracks)[src_at]
            returned = flame.delete(victim, confirm=False)
            out['returned']['delete'] = _ja(returned)
            deleted = True
    except BaseException as exc:
        out['exception'] = type(exc).__name__ + ': ' + str(exc)
    failed = out['exception'] is not None or problem is not None
    cleanup = {'clip': None, 'track': None}
    out['cleanup'] = cleanup
    if failed and created and not deleted and out['stage'] != 'delete':
        cleanup['track'] = _tm_remove_new_track(seq, version, new_at, sigs_before)
        if not cleanup['track']['deleted']:
            out['warnings'].append(
                'the track create_track added could not be removed again: ' + str(
                    cleanup['track']['error']
                )
            )
    if media:
        out['display'] = _tl_copy_display(shown_before)
        if attempted:
            try:
                new = _dm_new_entries(before_uids, _dm_media_children(reel))
            except BaseException:
                pass
            if made is None and new:
                made = _tl_pick_new(new, got)
            cleanup['clip'] = _td_cleanup(made, dest_entry, new, got)
            if cleanup['clip']['error'] is not None:
                out['warnings'].append('by-product of the call: ' + cleanup['clip']['error'])
    after_count = len(list(list(seq.versions)[version].tracks))
    sigs_after = [_tm_sig(seq, version, i) for i in range(after_count)]
    wanted = list(sigs_before)
    wanted.pop(track)
    wanted.insert(to, sigs_before[track])
    order = list(range(count))
    order.insert(to, order.pop(track))
    problems = _tm_problems(wanted, sigs_after)
    out['after'] = {'tracks': _tm_view(seq, version)}
    out['verification'] = {'ok': not problems, 'expectedOrder': order, 'problems': problems}
    audio_changed = _tb_audio_judge(out, audio_before, seq)
    video_changed = _tl_fingerprint(seq)['fingerprint'] != fp_before or after_count != count
    out['changed'] = video_changed or audio_changed
    out['notices'].append(
        'audio track positions cannot be moved through this op: no method to move or reorder '
        'audio tracks was found on PySequence or PyAudioTrack in Flame 2025.2.4 (listed). '
        'The audio tracks are not touched, '
        'result.audio reads them before and after'
    )
    out['notices'].append(
        'segment connections are kept by Flame (measured), and journal.revert restores this '
        'sequence only'
    )
    if audio_changed:
        out['warnings'].append(
            'the audio tracks changed although this op does not touch them: read result.audio'
        )
    if failed:
        what = out['exception'] if out['exception'] is not None else problem
        removed = cleanup['track'] is not None and cleanup['track']['deleted']
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            'moving track ' + str(track) + ' to ' + str(to) + ' stopped at ' + out['stage'] + ': '
            + what
            + ('' if deleted or out['stage'] == 'delete' else '; the source track was NOT deleted')
            + ('; the track create_track added was removed again' if removed else '')
            + ' (the sequence '
            + ('CHANGED: read result.after' if out['changed'] else 'is as it was')
            + ', journal.revert puts it back)',
        )
    if problems:
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            'the tracks read back are not the old order with track ' + str(track) + ' moved to '
            + str(to) + ': ' + '; '.join(problems) + ' (journal.revert puts the sequence back)',
        )
    if not out['changed'] and sigs_before != wanted:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            'the move returned and nothing in the sequence changed',
        )
    return out
"""
