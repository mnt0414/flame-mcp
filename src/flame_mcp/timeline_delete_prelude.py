"""Prelude source for `timeline.delete_segments` (`ops/timeline_build.py`).

Like `timeline_build_prelude`: real Python source that `execution.build_exec_code` places ahead
of an op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`,
`_OpError`, `_segment_list` and the `_tl_*` / `_tb_*` / `_dm_*` / `_dc_*` / `_ex_*` / `_sc_*`
helpers already defined.

What it encodes (measured on Flame 2025.2.4; the extract first with the sequence open in the
Timeline, then lift, a sequence that is not shown in the Timeline, a Gap, several
non-adjacent segments and the revert, see below):
  - `PySegment.selected` is read and written: the segments with `selected` True are the ones
    `PySequence.extract_selection_to_media_panel(destination)` acts on. It REMOVES them and closes
    the hole by moving the later segments of THAT TRACK left (the other tracks stay). Consecutive
    selected segments come out as one block. A selected Gap is closed the same way and what comes
    back is an empty 0-frame sequence, not a clip. With a destination (a reel) it answers the new
    PyClip / PySequence made of the selection; without one it answers None.
  - the length afterwards (measured on two video tracks and an audio track of one Gap, 120 frames,
    and on a sequence whose audio ran to 720 frames): the sequence
    is as long as the end of the longest CONTENT (everything but a Gap) of any video track or audio
    channel, and the Gap at the end of every track follows it. The track that ends in content at the
    new end has no Gap; a track that was not selected has its Gap shortened or removed (a Gap that
    would start at the new end is gone); the track that was selected gets a Gap when its content
    ends short of the new end. The judgement therefore compares everything but the Gap at the end
    of each track, and checks that the Gap that is there ends at the new end.
    What was measured is a sequence that was as long as its longest content, with start_time 0.
    The length and the Gap at the end are NOT judged (a warning says so, the media segments are
    still compared) when: the start_time frame cannot be read or is not the first record_in of the
    content; the length before the call cannot be read or is not start_time + the end of the longest
    content (a sequence longer than its longest content, every track ending in a Gap, was not
    measured); the audio cannot be read completely (the audio is not compared either).
    The length wanted is the end of the longest content minus the start_time frame.
  - `lift_selection_to_media_panel` takes the same arguments (ripple=false). It removes the
    selected segment and leaves a Gap of the same length where it was; nothing after it moves
    (shifted 0) and the other tracks are untouched (measured).
  - both work on a sequence that is not shown in the Timeline, with the stash reel of the
    Timeline checkpoints as the destination. A Gap can be selected and extracted (it is closed).
    Several non-adjacent segments in one call work (shifted = the sum of the lengths removed in
    front of the last later segment). journal.revert after such a call restored the sequence (its
    fingerprint matched) and the connections were kept (measured).
  - the call acts on the sequence's whole selection, so what is selected is read from the API's
    members (`PySequence.selected_segments`, `selected_transitions`, `selected_markers`,
    `PyTrack.selected_transitions`, `PyAudioTrack.selected_transitions`). They are PyAttributes
    (measured on Flame 2025.2.4): iterating one raises TypeError, `get_value()` holds the list
    (`_td_members`). An audio track lists its segments only through `selected_segments`. Selected
    segments, video or audio, are deselected with `selected` False and put back afterwards; a
    selected transition or marker is not cleared by that, and nothing is guessed: the op is
    refused (PRECONDITION_UNMET) naming it.
  - the by-product (what the call made in the destination) is the only thing this code deletes
    (`flame.delete(obj, confirm=False)`); it is found as the destination's entry that was not there
    before the call, matched with the answer.
  - the destination is the stash reel of the Timeline checkpoints
    (`timeline_prelude._tl_stash_reel`), so no reel of the user's is touched. Whether the Timeline
    display moves on is read before and after, like a copy of a sequence (`_tl_copy_display`).

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

DELETE_SEGMENTS_HELPER = """
_TD_METHODS = {True: 'extract_selection_to_media_panel', False: 'lift_selection_to_media_panel'}
_TD_ROWS = 12
_TD_NOT_JUDGED = (
    'the length of the sequence and the Gap at the end of the tracks were not judged'
)


def _td_key(seg):
    return [
        _j(seg.type),
        _v(seg.source_name),
        seg.source_in.frame,
        seg.source_out.frame,
        seg.record_out.frame - seg.record_in.frame + 1,
        _try(lambda: _v(seg.name)),
    ]


def _td_selected(seg):
    return bool(_v(seg.selected))


def _td_set_selected(seg, value):
    attr = seg.selected
    if type(attr).__name__ == 'PyAttribute':
        attr.set_value(value)
    else:
        seg.selected = value
    return _td_selected(seg)


def _td_each(seq):
    for vi, ver in enumerate(list(seq.versions)):
        for ti, trk in enumerate(list(ver.tracks)):
            for seg in list(trk.segments):
                yield vi, ti, seg


def _td_members(member):
    # What a selected_* member holds. On Flame 2025.2.4 it is a PyAttribute (iterating it raises
    # TypeError: 'PyAttribute' object is not iterable) whose get_value() is the list; anything
    # without get_value() is taken as the list itself.
    getter = getattr(member, 'get_value', None)
    value = getter() if callable(getter) else member
    return [] if value is None else list(value)


def _td_unselectable(seq):
    # What is selected in the sequence that setting `selected` False on segments cannot clear (a
    # transition or a marker), or cannot be read: the extract acts on the whole selection, so
    # nothing is guessed. [] when only segments can be selected.
    found = []

    def count(label, read):
        try:
            n = len(_td_members(read()))
        except BaseException as exc:
            found.append(label + ' cannot be read (' + type(exc).__name__ + ': ' + str(exc) + ')')
            return
        if n:
            found.append(str(n) + ' selected in ' + label)

    count('PySequence.selected_transitions', lambda: seq.selected_transitions)
    count('PySequence.selected_markers', lambda: seq.selected_markers)
    for vi, ver in enumerate(list(seq.versions)):
        for ti, trk in enumerate(list(ver.tracks)):
            count(
                'PyTrack.selected_transitions (version ' + str(vi) + ', track ' + str(ti) + ')',
                lambda trk=trk: trk.selected_transitions,
            )
    audio = _try(lambda: _td_members(seq.audio_tracks))
    if audio is None:
        found.append('PySequence.audio_tracks cannot be read')
    for ai, atrk in enumerate(audio or []):
        count(
            'PyAudioTrack.selected_transitions (audio track ' + str(ai) + ')',
            lambda atrk=atrk: atrk.selected_transitions,
        )
    return found


def _td_snapshot(seq):
    # Every selected segment, video and audio: what the API lists (PySequence.selected_segments)
    # and what the video tracks flag. Each entry keeps the wrapper (to select it again), its key
    # and record_in, and (version, track) when it was found in a video track.
    entries = []
    for seg in _td_members(seq.selected_segments):
        entries.append({'seg': seg, 'key': _td_key(seg), 'in': seg.record_in.frame, 'where': None})
    for vi, ti, seg in _td_each(seq):
        if not _td_selected(seg):
            continue
        key = _td_key(seg)
        rin = seg.record_in.frame
        hit = None
        for e in entries:
            if e['where'] is None and e['key'] == key and e['in'] == rin:
                hit = e
                break
        if hit is None:
            hit = {'seg': seg, 'key': key, 'in': rin, 'where': None}
            entries.append(hit)
        hit['where'] = (vi, ti)
    return entries


def _td_select_targets(seq, version, track, record_ins, entries):
    # Nothing selected but the targets: every selected segment (video and audio) is set to False,
    # the targets to True, and the API's own list is read back. None when it holds exactly the
    # targets, else why not.
    for e in entries:
        _td_set_selected(e['seg'], False)
    for vi, ti, seg in _td_each(seq):
        if _td_selected(seg):
            _td_set_selected(seg, False)
    wanted = set(record_ins)
    targets = []
    for seg in _segment_list(seq, version, track):
        if seg.record_in.frame in wanted:
            _td_set_selected(seg, True)
            targets.append(repr(_td_key(seg) + [seg.record_in.frame]))
    now = sorted(
        repr(_td_key(seg) + [seg.record_in.frame]) for seg in _td_members(seq.selected_segments)
    )
    flagged = sorted(
        (vi, ti, seg.record_in.frame) for vi, ti, seg in _td_each(seq) if _td_selected(seg)
    )
    expected_flags = sorted((version, track, r) for r in record_ins)
    if now != sorted(targets) or flagged != expected_flags:
        return (
            'after selected was set, PySequence.selected_segments holds ' + str(len(now))
            + ' segment(s) and the video tracks flag (version, track, record_in) '
            + repr(flagged[:20]) + ', not exactly the ' + str(len(record_ins)) + ' target(s) '
            + repr(expected_flags[:20])
        )
    return None


def _td_current(seq):
    current = {}
    for vi, ti, seg in _td_each(seq):
        current.setdefault((vi, ti), []).append([seg, _td_key(seg), seg.record_in.frame])
    return current


def _td_claim(rows, claimed, wanted_key, expected_in, old_in):
    # the row of a track that is the segment which was selected: the same key (type, source, source
    # range, length, name) not claimed yet, at the position it should be at, else where it was,
    # else the first
    free = [i for i, row in enumerate(rows) if i not in claimed and row[1] == wanted_key]
    for position in (expected_in, old_in):
        for i in free:
            if rows[i][2] == position:
                return i
    return free[0] if free else None


def _td_restore_selection(seq, entries, version, track, removed, ripple):
    # Puts back the selection the segments had before. A video segment is looked for in its track
    # by its key, the position it should be at (a ripple moved the ones behind what was removed
    # left) or the one it had; a segment the call removed is not found and is not selected. An
    # audio segment is selected again through the wrapper that was read. Segments left in place
    # (a call that raised or did nothing: the targets too) get their selection back as well.
    out = {'before': len(entries), 'changed': 0, 'after': None, 'error': None}
    try:
        current = _td_current(seq)
        claimed = {}
        for e in entries:
            if e['where'] is None:
                continue
            rows = current.get(e['where'], [])
            shift = 0
            if ripple and e['where'] == (version, track):
                shift = sum(r['duration'] for r in removed if r['recordIn'] < e['in'])
            done = claimed.setdefault(e['where'], set())
            hit = _td_claim(rows, done, e['key'], e['in'] - shift, e['in'])
            if hit is not None:
                done.add(hit)
        for where, rows in current.items():
            done = claimed.get(where, set())
            for i, row in enumerate(rows):
                want = i in done
                if _td_selected(row[0]) != want:
                    _td_set_selected(row[0], want)
                    out['changed'] += 1
        for e in entries:
            if e['where'] is None and not _td_selected(e['seg']):
                _td_set_selected(e['seg'], True)
                out['changed'] += 1
        out['after'] = len(_td_members(seq.selected_segments))
    except BaseException as exc:
        out['error'] = type(exc).__name__ + ': ' + str(exc)
    return out


def _td_state(seq, version, track):
    summary = _tl_track_summary(seq, version, track)
    gaps = len([1 for s in _segment_list(seq, version, track) if _j(s.type) == 'Gap'])
    return {
        'segmentCount': summary['segmentCount'],
        'gapCount': gaps,
        'mediaCount': summary['segmentCount'] - gaps,
        'startFrame': summary['startFrame'],
        'endFrame': summary['endFrame'],
        'trailingGap': summary['trailingGap'],
        'sequenceDuration': _try(lambda: _j(seq.duration)),
    }


def _td_removed(index, seg):
    return {
        'index': index,
        'type': _j(seg.type),
        'recordIn': seg.record_in.frame,
        'recordOut': seg.record_out.frame,
        'duration': seg.record_out.frame - seg.record_in.frame + 1,
        'name': _try(lambda: _v(seg.name)),
        'sourceName': _v(seg.source_name),
    }


def _td_resolve(seq, version, track, record_ins, expect):
    # ([(index, segment)] in the order given, error code, message); the first problem wins
    segs = _segment_list(seq, version, track)
    found = []
    for n, rin in enumerate(record_ins):
        hits = [(i, s) for i, s in enumerate(segs) if s.record_in.frame == rin]
        label = 'segments[' + str(n) + '] = ' + str(rin)
        if len(hits) == 0:
            frames = [s.record_in.frame for s in segs]
            near = sorted(frames, key=lambda f: (abs(f - rin), f))[:5]
            return None, 'SELECTOR_EMPTY', (
                label + ': no segment with that record_in in this track (nearest record_in '
                'values: ' + str(near) + ')'
            )
        if len(hits) > 1:
            return None, 'SELECTOR_AMBIGUOUS', (
                label + ': ' + str(len(hits)) + ' segments with that record_in in this track'
            )
        if expect is not None:
            actual = _v(hits[0][1].source_name)
            if actual != expect[n]:
                return None, 'CONFLICT', (
                    label + ': the segment found has source_name ' + repr(actual)
                    + ', not the expected ' + repr(expect[n]) + '; nothing was done'
                )
        found.append(hits[0])
    return found, None, None


def _td_connected(seq, version, track, found):
    # the OTHER segments connected (PySegment.connected_segments(), minus the segment itself) to
    # each media segment to remove
    cache = {}
    rows = []
    total = 0
    unreadable = 0
    for index, seg in found:
        if _j(seg.type) == 'Gap':
            continue
        count, problem = _sc_others(seg, seq, version, track, cache)
        if count is None:
            unreadable += 1
        elif count > 0:
            total += count
            rows.append({'recordIn': seg.record_in.frame, 'count': count})
    return {'total': total, 'segments': rows, 'unreadable': unreadable}


def _td_delete_check(seq, version, track, record_ins, expect, ripple):
    out = _tb_new({'ripple': ripple, 'version': version, 'track': track, 'targets': None})
    if _tb_start(out, seq, 'delete_segments needs a sequence')['error'] is not None:
        return out
    method = _TD_METHODS[ripple]
    if not callable(getattr(seq, method, None)):
        return _tb_fail(
            out,
            'PRECONDITION_UNMET',
            'this Flame has no PySequence.' + method + ': nothing was done',
        )
    out['before'] = _td_state(seq, version, track)
    found, code, message = _td_resolve(seq, version, track, record_ins, expect)
    if message is not None:
        return _tb_fail(out, code, message)
    out['targets'] = [_td_removed(i, s) for i, s in found]
    unselectable = _td_unselectable(seq)
    if unselectable:
        return _tb_fail(
            out,
            'PRECONDITION_UNMET',
            'the sequence has a selection that selecting segments cannot clear: '
            + '; '.join(unselectable) + ' (the extract acts on everything that is selected; only '
            'selected segments are deselected here). Clear it in the Timeline first: nothing was '
            'done',
        )
    try:
        _td_selected(found[0][1])
    except BaseException as exc:
        return _tb_fail(
            out,
            'PRECONDITION_UNMET',
            'PySegment.selected cannot be read (' + type(exc).__name__ + ': ' + str(exc)
            + '): the segments cannot be selected, nothing was done',
        )
    return out


def _td_expected_media(rows, removed_index, ripple):
    # the media segments a track should have left: the others, each moved left by what was removed
    # in front of it when the hole is closed
    expected = []
    shift = 0
    for i, row in enumerate(rows):
        if i in removed_index:
            shift += (row[2] - row[1] + 1) if ripple else 0
        elif row[0] != 'Gap':
            expected.append([row[0], row[1] - shift, row[2] - shift] + row[3:])
    return expected


def _td_split_tail(rows):
    # (the rows without the Gap at the end, that Gap or None); a row is [type, record_in,
    # record_out, ...]
    if rows and rows[-1][0] == 'Gap':
        return rows[:-1], rows[-1]
    return rows, None


def _td_content_end(rows):
    # the frame after the last content (everything but a Gap) of a track, None when it has none
    ends = [row[2] + 1 for row in rows if row[0] != 'Gap']
    return max(ends) if ends else None


def _td_tail_problem(label, rows, end):
    # None when the Gap at the end of the track (if there is one) ends at end - 1 and follows the
    # row before it
    body, tail = _td_split_tail(rows)
    if tail is None:
        return None
    if tail[2] != end - 1:
        return (
            label + ': the Gap at the end ends at frame ' + str(tail[2]) + ', not at '
            + str(end - 1) + ' (the end of the longest content, where the sequence now ends)'
        )
    if body and tail[1] != body[-1][2] + 1:
        return (
            label + ': the Gap at the end starts at frame ' + str(tail[1]) + ', not at '
            + str(body[-1][2] + 1) + ' (right behind the segment before it)'
        )
    return None


def _td_audio_channels(snap):
    # {(audio track, channel): [[type, record_in, record_out, source_name, source_in, source_out]]}
    found = {}
    for row in snap['rows']:
        found.setdefault((row[0], row[1]), []).append(
            [row[2], row[4], row[5], row[3], row[6], row[7]]
        )
    return found


def _td_unmeasured(start, length_before, rows_before, others_before, channels_before):
    # None when the sequence is of the kind whose length was measured: it starts at its
    # start_time frame (the first record_in of its content) and was as long as its longest content
    # ended. Else what is different, in a sentence.
    ends = [_td_content_end(rows_before)]
    ends += [_td_content_end(rows) for key, rows in others_before]
    ends += [_td_content_end(rows) for rows in channels_before.values()]
    ends = [e for e in ends if e is not None]
    firsts = [row[1] for row in rows_before if row[0] != 'Gap']
    firsts += [row[1] for key, rows in others_before for row in rows if row[0] != 'Gap']
    firsts += [row[1] for rows in channels_before.values() for row in rows if row[0] != 'Gap']
    if start is None:
        return 'the start_time of the sequence could not be read, so ' + _TD_NOT_JUDGED
    if firsts and min(firsts) != start:
        return (
            'the start_time of the sequence (frame ' + str(start) + ') is not the first '
            'record_in of its content (frame ' + str(min(firsts)) + '), so ' + _TD_NOT_JUDGED
        )
    if length_before is None:
        return (
            'the length of the sequence before the call could not be read, so ' + _TD_NOT_JUDGED
        )
    if ends and start + length_before != max(ends):
        return (
            'the sequence was ' + str(length_before) + ' frame(s) long before (it ended before '
            'frame ' + str(start + length_before) + ') and its longest content ended before '
            'frame ' + str(max(ends)) + ' (a sequence longer than its longest content, every '
            'track ending in a Gap), so ' + _TD_NOT_JUDGED
        )
    return None


def _td_judge(seq, where, rows_before, rows_after, removed_index, others_before, ripple, audio,
              duration, start, length_before):
    # ([what is wrong], [what was not judged and why]). `where` is (version, track), `audio`
    # (before, after) as _tb_audio_read answers them, `duration` the frames the sequence has now
    # (None: not read), `start` the start_time frame and `length_before` the frames the sequence
    # had before the call (None: not read).
    problems = []
    notes = []
    expected = _td_expected_media(rows_before, removed_index, ripple)
    have = [row for row in rows_after if row[0] != 'Gap']
    if have != expected:
        problems.append(
            'the media segments left in the track are ' + repr(have[:_TD_ROWS]) + ' [type, '
            'record_in, record_out, source_in, source_out, head, tail], not '
            + repr(expected[:_TD_ROWS])
        )
    if not ripple:
        for key, rows in others_before:
            now = _tl_track_rows(seq, key[0], key[1])
            if now != rows:
                problems.append(
                    'version ' + str(key[0]) + ' track ' + str(key[1]) + ' changed: '
                    + repr(now[:_TD_ROWS]) + ', it was ' + repr(rows[:_TD_ROWS])
                )
        return problems, notes
    before_snap, after_snap = audio
    audio_read = not before_snap['errors'] and not after_snap['errors']
    channels_before = _td_audio_channels(before_snap) if audio_read else {}
    channels_after = _td_audio_channels(after_snap) if audio_read else {}
    ends = [_td_content_end(expected)]
    ends += [_td_content_end(rows) for key, rows in others_before]
    ends += [_td_content_end(rows) for rows in channels_before.values()]
    ends = [e for e in ends if e is not None]
    end = max(ends) if ends and audio_read else None
    if not audio_read:
        notes.append(
            'part of the audio could not be read, so for ripple the audio, ' + _TD_NOT_JUDGED
            + ' (the media segments of the video tracks were compared)'
        )
    elif end is not None:
        reason = _td_unmeasured(start, length_before, rows_before, others_before, channels_before)
        if reason is not None:
            notes.append(reason)
            end = None
    label = 'version ' + str(where[0]) + ' track ' + str(where[1])
    tail_problem = None if end is None else _td_tail_problem(label, rows_after, end)
    if tail_problem is not None:
        problems.append(tail_problem)
    for key, rows in others_before:
        now = _tl_track_rows(seq, key[0], key[1])
        label = 'version ' + str(key[0]) + ' track ' + str(key[1])
        if _td_split_tail(now)[0] != _td_split_tail(rows)[0]:
            problems.append(
                label + ' changed: ' + repr(now[:_TD_ROWS]) + ', it was ' + repr(rows[:_TD_ROWS])
            )
        elif end is not None:
            tail_problem = _td_tail_problem(label, now, end)
            if tail_problem is not None:
                problems.append(tail_problem)
    for key in sorted(set(channels_before) | set(channels_after)):
        rows = channels_before.get(key, [])
        now = channels_after.get(key, [])
        label = 'audio track ' + str(key[0]) + ' channel ' + str(key[1])
        if _td_split_tail(now)[0] != _td_split_tail(rows)[0]:
            problems.append(
                label + ' changed: ' + repr(now[:_TD_ROWS]) + ', it was ' + repr(rows[:_TD_ROWS])
            )
        elif end is not None:
            tail_problem = _td_tail_problem(label, now, end)
            if tail_problem is not None:
                problems.append(tail_problem)
    if end is not None and duration is not None:
        wanted = end - start
        if duration != wanted:
            problems.append(
                'the sequence is ' + str(duration) + ' frame(s) long, not the ' + str(wanted)
                + ' it should be (it ends where the longest content ends, frame ' + str(end - 1)
                + ')'
            )
    return problems, notes


def _td_other_rows(seq, version, track):
    rows = []
    for vi, ver in enumerate(list(seq.versions)):
        for ti in range(len(list(ver.tracks))):
            if (vi, ti) != (version, track):
                rows.append(((vi, ti), _tl_track_rows(seq, vi, ti)))
    return rows


def _td_cleanup(made, dest_entry, new, got):
    # deletes the by-product, and only that: the one new entry of the destination (matched with
    # the answer when there are several). Answers {attempted, deleted, returned, error}.
    out = {'attempted': False, 'deleted': False, 'returned': None, 'error': None}
    uid = None if made is None else _dm_uid(made)
    if made is None:
        if new:
            out['error'] = (
                str(len(new)) + ' new entries appeared in the stash reel and none of them can be '
                'told to be what the call made (names ' + repr(_dc_names(new)) + '): nothing '
                'was deleted'
            )
        else:
            out['error'] = (
                'the call answered ' + str(len(got)) + ' object(s) and the stash reel did not '
                'grow: there is nothing to delete'
            )
        return out
    if type(made).__name__ not in _DC_ENTRY_TYPES or uid is None:
        out['error'] = (
            'the new entry is a ' + type(made).__name__ + ' with uid ' + repr(uid)
            + ': it was NOT deleted'
        )
        return out
    out['attempted'] = True
    try:
        gone, returned, still = _dm_delete(made, uid, dest_entry)
        out['deleted'] = gone
        out['returned'] = _ja(returned)
        if not gone:
            out['error'] = (
                'flame.delete returned ' + repr(out['returned']) + ' and the object is '
                + ('still there' if still is not None else 'gone')
            )
    except BaseException as exc:
        out['error'] = 'flame.delete raised ' + type(exc).__name__ + ': ' + str(exc)
    return out


def _td_delete(seq, version, track, record_ins, expect, ripple, library_name, reel_name):
    out = _td_delete_check(seq, version, track, record_ins, expect, ripple)
    if out['error'] is not None:
        return out
    found = _td_resolve(seq, version, track, record_ins, expect)[0]
    removed_index = [i for i, s in found]
    method = _TD_METHODS[ripple]
    rows_before = _tl_track_rows(seq, version, track)
    others_before = _td_other_rows(seq, version, track)
    fp_before = _tl_fingerprint(seq)['fingerprint']
    audio_before = _tb_audio_read(seq)
    start = _try(lambda: _v(seq.start_time).frame)
    length_before = _try(lambda: _v(seq.duration).frame)
    removed = out['targets']
    connected = _td_connected(seq, version, track, found)
    survivors = [i for i in range(len(rows_before)) if i not in removed_index]
    last = survivors[-1] if survivors else -1
    out.update(
        method=method,
        removed=removed,
        shifted=sum(r['duration'] for r in removed if r['index'] < last) if ripple else 0,
        connected=connected,
        after=None,
        changed=None,
        returned=None,
        extracted=None,
        cleanup=None,
        selection=None,
        display=None,
        exception=None,
        audio=None,
        notices=[],
        warnings=[],
    )
    out.pop('targets')
    if connected['total'] > 0:
        out['notices'].append(
            str(connected['total']) + ' other segment(s) are connected to the segment(s) removed '
            '(PySegment.connected_segments(), the segment itself left out). Measured, for a copy '
            'whose segments were connected to an original and run through this op: it left the '
            'original sequence unchanged. journal.revert restores this sequence only'
        )
    lib, reel, out['libraryCreated'], out['reelCreated'] = _tl_stash_reel(library_name, reel_name)
    out['stash'] = {'library': _try(lambda: _v(lib.name)), 'reel': _try(lambda: _v(reel.name))}
    dest_entry = _dc_with_selector(reel)
    before_uids = [_dm_uid(o) for o in _dm_media_children(reel)]
    entries = _td_snapshot(seq)
    shown_before = _tl_shown()
    returned = None
    problem = None
    what = 'selecting the segments'
    try:
        problem = _td_select_targets(seq, version, track, record_ins, entries)
        if problem is None:
            out['ran'] = True
            what = method + '()'
            returned = getattr(seq, method)(reel)
    except BaseException as exc:
        out['exception'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        out['display'] = _tl_copy_display(shown_before)
        out['selection'] = _td_restore_selection(seq, entries, version, track, removed, ripple)
        if out['selection']['error'] is not None:
            out['warnings'].append(
                'the selection the segments had before could not be put back completely: '
                + out['selection']['error']
            )
    if problem is not None:
        out['ran'] = False
        return _tb_fail(out, 'FLAME_API_ERROR', problem + ': nothing was removed')
    got, shape = _ex_answer(returned)
    out['returned'] = {'shape': shape, 'types': [type(o).__name__ for o in got]}
    new = _dm_new_entries(before_uids, _dm_media_children(reel))
    made = _tl_pick_new(new, got) if new else None
    if made is not None:
        out['extracted'] = {
            'type': type(made).__name__,
            'name': _try(lambda: _v(made.name)),
            'uid': _dm_uid(made),
            'duration': _try(lambda: _j(made.duration)),
        }
    out['cleanup'] = _td_cleanup(made, dest_entry, new, got)
    if out['cleanup']['error'] is not None:
        out['warnings'].append('by-product of the call: ' + out['cleanup']['error'])
    rows_after = _tl_track_rows(seq, version, track)
    out['after'] = _td_state(seq, version, track)
    video_changed = _tl_fingerprint(seq)['fingerprint'] != fp_before
    audio_changed = _tb_audio_judge(out, audio_before, seq)
    audio_after = _tb_audio_read(seq)
    duration = _try(lambda: _v(seq.duration).frame)
    out['changed'] = video_changed or audio_changed
    if out['exception'] is not None:
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            what + ' raised ' + out['exception'] + ' (the track '
            + ('CHANGED: read result.after' if out['changed'] else 'is unchanged') + ')',
        )
    if not out['changed']:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            method + '() returned ' + repr(out['returned']['shape']) + ' and nothing in the '
            'sequence changed',
        )
    _tb_audio_only(out, video_changed, audio_changed)
    if ripple and duration is None:
        out['warnings'].append(
            'the length of the sequence could not be read: it was not checked against the end of '
            'the longest content'
        )
    problems, notes = _td_judge(
        seq,
        (version, track),
        rows_before,
        rows_after,
        removed_index,
        others_before,
        ripple,
        (audio_before, audio_after),
        duration,
        start,
        length_before,
    )
    out['warnings'].extend(notes)
    if problems and ripple:
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            'the sequence changed, but not the way ' + method + ' is measured to change it: '
            + '; '.join(problems),
        )
    for text in problems:
        out['warnings'].append(
            method + ' did not leave the media segments where they were (measured so on Flame '
            '2025.2.4): ' + text
        )
    return out
"""
