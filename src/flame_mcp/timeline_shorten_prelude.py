"""Prelude source for `timeline.shorten` (`ops/timeline_shorten.py`).

Like `timeline_delete_prelude`: real Python source that `execution.build_exec_code` places ahead of
an op's own code. It runs inside Flame (3.11) with `flame`, `_v`, `_j`, `_ja`, `_try`, `_OpError`,
`_segment_list` and the `_tl_*` / `_tb_*` / `_dm_*` / `_dc_*` / `_ex_*` / `_td_*` / `_tm_*` helpers
already defined.

What it encodes (measured on Flame 2025.2.4, a sequence of 720 frames shortened to 360
with two audio channels, an audio track whose length decided the length of the sequence, and video
tracks that ended in a Gap):
  - the audio segments of the sequence are reachable only as
    `seq.audio_tracks[i].channels[j].segments`. `PySegment.trim_tail` on an audio segment turns it
    into a Gap and `smart_replace_media` on one crashes Flame: neither is used.
  - `PyTrack.cut(time)` cuts a Gap too, and the channel of an audio track (what the segments of
    `channels[j].segments` belong to) takes `cut(time)` as well. `time` is the absolute record
    frame, `flame.PyTime(frame, seq.frame_rate)`; it becomes the first frame of the right part.
  - after the cuts every segment that starts at the end (record_in >= end) is selected
    (`PySegment.selected = True`: a video Gap, a video media segment, an audio segment) and ONE
    `PySequence.extract_selection_to_media_panel(reel)` removes them; each track closes up on its
    own. The length of the sequence became the new one and the audio stayed Audio Segments.
  - the by-product clip of the extract is the only thing deleted, found as the one new entry of the
    stash reel (`_td_cleanup`, shared with `timeline.delete_segments`), and the selection the
    segments had is put back as far as they can be followed (`_td_restore_selection`).
  - the selection is read AFTER the cuts (the objects of the cut segments are new ones); the number
    that was selected before the cuts is kept, and a difference is a warning.
  - a selected segment that the extract removed (record_in >= end, video or audio) is not put back:
    it is counted in `selection.dropped`, and no look-alike (same key) is selected in its place.
    When the tail is still there (the extract did nothing), nothing is dropped.
  - `PyTrack.transitions` lists a 'Cut' of duration 0 at every segment boundary; the transition at
    the boundary at `end` (index i - 1 for the segment i that starts at `end`) must be one of
    those, else the op is refused: a dissolve there was NOT measured. The same goes for the
    transitions of an audio channel at that boundary.
  - a Gap that crossed `end` comes out of the cut with its source_in counted from 0 again and its
    name '' (it was 'untitled'; measured): the readback compares a Gap by type, record
    range and effect types only.
NOT measured, and so refused or left to the readback: two or more versions, a stereo_linked video
track, a video media segment cut at `end` (the Gap and audio cuts were measured; `timeline.cut`
measured the media one) and a transition that starts before `end` and reaches past it.
Measured on a video track that ENDS IN MEDIA (Flame 2025.2.4 and 2026.2.3): a sequence of 72
frames shortened to 60 left track 0 = p2_av72 0-59 with no Gap at the end, track 1 = clip1 0-23,
Gap 24-47, clip2 48-59, and both audio channels one Audio Segment 0-59; the extract succeeded and
`journal.revert` put the fingerprint, the video rows, the audio rows and the length 72 back. On the
sequence whose video tracks ended in a Gap and whose audio crossed the end (72 frames, 36 asked)
the revert brought back the length, the video rows and the audio rows too.

The record frames are absolute: `end = start_time.frame + duration`. What was measured is a
sequence whose start_time is 0 (video and audio both start at record_in 0, the last audio
record_out is the length minus 1). A start_time that is not 0 is run only when the first record_in
of the video tracks AND of every audio channel is start_time; any other start is refused (the audio
start of such a sequence was not measured).

Every function answers a dict with `error` / `errorCode` (None when fine); the op's own code turns
an error into `_OpError` so the partial result stays in the envelope.
"""

from __future__ import annotations

SHORTEN_HELPER = """
_TS_REMOVED_ROWS = 20
_TS_ROWS = 6
_TS_V = {'type': 0, 'rin': 1, 'rout': 2, 'sout': 4, 'gap': (0, 1, 2, 7)}
_TS_A = {'type': 2, 'rin': 4, 'rout': 5, 'sout': 7, 'gap': (2, 4, 5)}


def _ts_vrow(seg):
    # [type, record_in, record_out, source_in, source_out, source_name, name, effect types]
    return [
        _j(seg.type),
        seg.record_in.frame,
        seg.record_out.frame,
        seg.source_in.frame,
        seg.source_out.frame,
        _v(seg.source_name),
        _try(lambda: _v(seg.name)),
        _try(lambda: [_j(fx.type) for fx in list(seg.effects)]),
    ]


def _ts_where(place):
    if place['media'] == 'video':
        return 'video track ' + str(place['track'])
    return 'audio track ' + str(place['track']) + ' channel ' + str(place['channel'])


def _ts_places(seq):
    # every thing that can be cut: the video tracks of version 0, then every channel of every audio
    # track. 'obj' is what takes cut(), 'segs' the segments read now.
    places = []
    versions = list(seq.versions)
    for ti, trk in enumerate(list(versions[0].tracks)):
        places.append({'media': 'video', 'track': ti, 'channel': None, 'obj': trk,
                       'segs': list(trk.segments)})
    try:
        audio = list(seq.audio_tracks)
    except AttributeError:
        audio = []
    for ai, atrk in enumerate(audio):
        for ci, chan in enumerate(list(atrk.channels)):
            places.append({'media': 'audio', 'track': ai, 'channel': ci, 'obj': chan,
                           'segs': list(chan.segments)})
    return places


def _ts_boundary_refusal(place, end):
    # why the transition at the boundary at `end` of this place is not one that was measured, or
    # None. A boundary exists when a segment other than the first starts at `end`.
    index = None
    for i, seg in enumerate(place['segs']):
        if i > 0 and seg.record_in.frame == end:
            index = i
    if index is None:
        return None
    where = _ts_where(place)
    try:
        transitions = list(place['obj'].transitions)
    except BaseException as exc:
        return (
            'the transitions of ' + where + ' cannot be read (' + type(exc).__name__ + ': '
            + str(exc) + '), so it is not known what is at the boundary at ' + str(end)
            + ': nothing was done'
        )
    if index - 1 >= len(transitions):
        return (
            where + ' lists ' + str(len(transitions)) + ' transition(s) but has a boundary at '
            + str(end) + ' (segment ' + str(index) + '), so the transition at that boundary '
            'cannot be told: nothing was done'
        )
    try:
        kind, duration = _tm_transition(transitions[index - 1])
    except BaseException as exc:
        return (
            'the transition at the boundary at ' + str(end) + ' of ' + where + ' cannot be read ('
            + type(exc).__name__ + ': ' + str(exc) + '): nothing was done'
        )
    if kind != 'Cut' or duration != 0:
        return (
            'the transition at the boundary at ' + str(end) + ' of ' + where + ' is not a Cut of '
            'duration 0 (type ' + repr(kind) + ', duration ' + repr(duration) + '): nothing was '
            'done'
        )
    return None


def _ts_plan(places, end):
    # per place: how many segments cross `end` (they are cut) and how many start at or after it
    # (with those cut, they are removed), and the media that goes, as rows
    tracks = []
    media = []
    for place in places:
        crossing = [s for s in place['segs'] if s.record_in.frame < end <= s.record_out.frame]
        later = [s for s in place['segs'] if s.record_in.frame >= end]
        place['cut'] = bool(crossing)
        tracks.append({
            'media': place['media'],
            'track': place['track'],
            'channel': place['channel'],
            'cut': bool(crossing),
            'removed': len(crossing) + len(later),
        })
        for seg in crossing + later:
            if _j(seg.type) == 'Gap':
                continue
            media.append({
                'media': place['media'],
                'track': place['track'],
                'channel': place['channel'],
                'sourceName': _v(seg.source_name),
                'recordIn': max(seg.record_in.frame, end),
                'recordOut': seg.record_out.frame,
            })
    return tracks, media


def _ts_inspect(seq, duration):
    # (out, places, end). out['error'] is set when the op cannot go on; nothing is written here.
    out = _tb_new({'duration': duration, 'end': None, 'startFrame': None, 'tracks': None})
    if _tb_start(out, seq, 'shorten needs a sequence')['error'] is not None:
        return out, None, None
    if not callable(getattr(seq, 'extract_selection_to_media_panel', None)):
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'this Flame has no PySequence.extract_selection_to_media_panel: nothing was done')
        return out, None, None
    try:
        length = _v(seq.duration).frame
        base = _v(seq.start_time).frame
    except BaseException as exc:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the duration or the start_time of the sequence cannot be read ('
                 + type(exc).__name__ + ': ' + str(exc) + '): nothing was done')
        return out, None, None
    out['before'] = {'duration': length, 'startFrame': base}
    out['startFrame'] = base
    if duration >= length:
        _tb_fail(out, 'INVALID_ARGS',
                 'duration ' + str(duration) + ' does not lengthen the sequence (it is '
                 + str(length) + ' frames long): this op only shortens, nothing was done')
        return out, None, None
    versions = list(seq.versions)
    if len(versions) != 1:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the sequence has ' + str(len(versions)) + ' versions and exactly one is '
                 'needed: nothing was done')
        return out, None, None
    audio = _tb_audio_read(seq)
    if audio['errors']:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the audio cannot be read completely (' + '; '.join(audio['errors'][:_TS_ROWS])
                 + '), so the result could not be judged: nothing was done')
        return out, None, None
    try:
        places = _ts_places(seq)
    except BaseException as exc:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the tracks cannot be read (' + type(exc).__name__ + ': ' + str(exc)
                 + '): nothing was done')
        return out, None, None
    firsts = [p['segs'][0].record_in.frame for p in places if p['media'] == 'video' and p['segs']]
    if firsts and min(firsts) != base:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the start of the sequence (start_time frame ' + str(base) + ') is not the first '
                 'record_in of its video tracks (' + str(min(firsts)) + '), so the end frame '
                 'cannot be told: nothing was done')
        return out, None, None
    late = [
        _ts_where(p) + ' starts at ' + str(p['segs'][0].record_in.frame)
        for p in places if p['media'] == 'audio' and p['segs']
        and p['segs'][0].record_in.frame != base
    ]
    if late:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the audio does not start at the start_time frame ' + str(base) + ' ('
                 + '; '.join(late[:_TS_ROWS]) + '), so the end frame cannot be told for it: '
                 'nothing was done')
        return out, None, None
    end = base + duration
    out['end'] = end
    for place in places:
        if place['media'] != 'video':
            continue
        try:
            linked = _v(place['obj'].stereo_linked)
        except BaseException as exc:
            _tb_fail(out, 'PRECONDITION_UNMET',
                     'PyTrack.stereo_linked of ' + _ts_where(place) + ' cannot be read ('
                     + type(exc).__name__ + ': ' + str(exc) + '), and a stereo_linked track is '
                     'refused: nothing was done')
            return out, None, None
        if linked:
            _tb_fail(out, 'PRECONDITION_UNMET',
                     _ts_where(place) + ' is stereo_linked, which is refused: nothing was done')
            return out, None, None
    for place in places:
        problem = _ts_boundary_refusal(place, end)
        if problem is not None:
            _tb_fail(out, 'PRECONDITION_UNMET', problem)
            return out, None, None
    out['tracks'], media = _ts_plan(places, end)
    out['removedMediaCount'] = len(media)
    out['removedMedia'] = media[:_TS_REMOVED_ROWS]
    out['removedMediaTruncated'] = len(media) > _TS_REMOVED_ROWS
    for place in places:
        if place['media'] == 'audio' and place['cut'] and not callable(
                getattr(place['obj'], 'cut', None)):
            _tb_fail(out, 'PRECONDITION_UNMET',
                     'this Flame has no PyAudioChannel.cut for ' + _ts_where(place)
                     + ': nothing was done')
            return out, None, None
    if not [t for t in out['tracks'] if t['removed'] > 0]:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'no track has a segment at or after the end frame ' + str(end)
                 + ': there is nothing to remove, nothing was done')
        return out, None, None
    unselectable = _td_unselectable(seq)
    if unselectable:
        _tb_fail(out, 'PRECONDITION_UNMET',
                 'the sequence has a selection that selecting segments cannot clear: '
                 + '; '.join(unselectable) + ' (the extract acts on everything that is selected; '
                 'only selected segments are deselected here). Clear it in the Timeline first: '
                 'nothing was done')
        return out, None, None
    for place in places:
        if place['segs']:
            try:
                _td_selected(place['segs'][0])
            except BaseException as exc:
                _tb_fail(out, 'PRECONDITION_UNMET',
                         'PySegment.selected cannot be read (' + type(exc).__name__ + ': '
                         + str(exc) + '): the segments cannot be selected, nothing was done')
                return out, None, None
            break
    return out, places, end


def _ts_check(seq, duration):
    return _ts_inspect(seq, duration)[0]


def _ts_select(seq, targets, entries):
    # Nothing selected but the targets: every selected segment is set to False, the targets to
    # True, and the API's own list is read back. None when it holds exactly the targets, else why
    # not. `targets` = [(place, segment)].
    for e in entries:
        _td_set_selected(e['seg'], False)
    for vi, ti, seg in _td_each(seq):
        if _td_selected(seg):
            _td_set_selected(seg, False)
    for place, seg in targets:
        _td_set_selected(seg, True)
    wanted = sorted(repr(_td_key(seg) + [seg.record_in.frame]) for place, seg in targets)
    now = sorted(
        repr(_td_key(seg) + [seg.record_in.frame]) for seg in _td_members(seq.selected_segments)
    )
    flagged = sorted((ti, seg.record_in.frame) for vi, ti, seg in _td_each(seq)
                     if _td_selected(seg))
    expected = sorted((place['track'], seg.record_in.frame) for place, seg in targets
                      if place['media'] == 'video')
    if now != wanted or flagged != expected:
        return (
            'after selected was set, PySequence.selected_segments holds ' + str(len(now))
            + ' segment(s) and the video tracks flag (track, record_in) ' + repr(flagged[:20])
            + ', not exactly the ' + str(len(targets)) + ' target(s) (' + repr(expected[:20])
            + ' on video tracks)'
        )
    return None


def _ts_expected(rows, end, at):
    # [(row, is the piece of a segment that crossed end)] the rows that should be left: the ones
    # entirely before end as they were, a crossing one cut to end - 1
    expected = []
    for row in rows:
        if row[at['rout']] < end:
            expected.append((row, False))
        elif row[at['rin']] < end:
            piece = list(row)
            piece[at['rout']] = end - 1
            expected.append((piece, True))
    return expected


def _ts_compare(label, before, after, end, at):
    # [problem] for one track or channel: what is left must be what _ts_expected says. The
    # source_out of a piece that was cut is not compared (a Timewarp or a speed change moves it).
    # A Gap (before and after) is compared by type, record range and effect types only: the cut
    # counts its source_in from 0 again and clears its name (measured).
    expected = _ts_expected(before, end, at)
    if len(expected) != len(after):
        return [
            label + ' has ' + str(len(after)) + ' segment(s) ' + repr(after[:_TS_ROWS])
            + ', not the ' + str(len(expected)) + ' expected '
            + repr([e[0] for e in expected][:_TS_ROWS])
        ]
    for i, (want, crossed) in enumerate(expected):
        have = list(after[i])
        want = list(want)
        if have[at['type']] == 'Gap' and want[at['type']] == 'Gap':
            have = [have[k] for k in at['gap']]
            if have != [want[k] for k in at['gap']]:
                return [
                    label + ' segment ' + str(i) + ' is ' + repr(after[i]) + ', not '
                    + repr(want) + ' (a Gap is compared by type, record range and effects only)'
                ]
            continue
        if crossed:
            have[at['sout']] = None
            want[at['sout']] = None
        if have != want:
            return [
                label + ' segment ' + str(i) + ' is ' + repr(after[i]) + ', not ' + repr(want)
            ]
    return []


def _ts_judge(seq, places_before, rows_before, audio_before, end, duration):
    problems = []
    now = _try(lambda: _v(seq.duration).frame)
    if now != duration:
        problems.append('the sequence duration is ' + repr(now) + ' frames, not the '
                        + str(duration) + ' asked for')
    audio_after = _tb_audio_read(seq)
    if audio_after['errors']:
        problems.append('the audio cannot be read back (' + '; '.join(audio_after['errors'][:3])
                        + ')')
    after_places = _ts_places(seq)
    for place in after_places:
        if place['media'] == 'video':
            after = [_ts_vrow(s) for s in place['segs']]
            before = rows_before[('video', place['track'], None)]
            at = _TS_V
        else:
            ai, ci = place['track'], place['channel']
            after = [r for r in audio_after['rows'] if r[0] == ai and r[1] == ci]
            before = [r for r in audio_before['rows'] if r[0] == ai and r[1] == ci]
            at = _TS_A
        problems += _ts_compare(_ts_where(place), before, after, end, at)
    if len(after_places) != len(places_before):
        problems.append('the sequence has ' + str(len(after_places)) + ' track(s) / channel(s) '
                        'now, it had ' + str(len(places_before)))
    return problems


def _ts_state(seq):
    return {'duration': _try(lambda: _v(seq.duration).frame)}


def _ts_cut_all(seq, places, end):
    # cuts every place that has a segment crossing `end`; None when all went, else the message
    stamp = _tb_time(seq, end)
    for place in places:
        if not place['cut']:
            continue
        try:
            place['obj'].cut(stamp)
        except BaseException as exc:
            return ('cutting ' + _ts_where(place) + ' at ' + str(end) + ' raised '
                    + type(exc).__name__ + ': ' + str(exc))
    return None


def _ts_uncut(seq, places, end):
    # the cut places that have no segment starting at `end` now
    missed = []
    for place in _ts_places(seq):
        mine = [p for p in places if p['media'] == place['media'] and p['track'] == place['track']
                and p['channel'] == place['channel']][0]
        if mine['cut'] and not [s for s in place['segs'] if s.record_in.frame == end]:
            missed.append(_ts_where(place))
    return missed


def _ts_fail_after_cuts(out, seq, fp_before, audio_before, code, message):
    # a failure when the cuts may have been made but nothing was removed: reads the sequence
    # again so that the answer says whether it CHANGED
    video_changed = _tl_fingerprint(seq)['fingerprint'] != fp_before
    audio_changed = _tb_audio_judge(out, audio_before, seq)
    changed = video_changed or audio_changed
    out['changed'] = changed
    out['after'] = _ts_state(seq)
    return _tb_fail(
        out,
        code,
        message + ' (the sequence ' + ('CHANGED: read result.after and result.audio'
                                       if changed else 'is unchanged')
        + '); nothing was removed',
    )


def _ts_tail_gone(seq, end):
    # True when no segment of any place starts at or after `end` any more: the extract took them
    return not [s for p in _ts_places(seq) for s in p['segs'] if s.record_in.frame >= end]


def _ts_deselect_audio(targets):
    # the audio targets are deselected by hand (the video tracks are cleared with the selection
    # put back); the first failure and how many there were, or None
    failures = []
    for place, seg in targets:
        if place['media'] == 'audio':
            try:
                if _td_selected(seg):
                    _td_set_selected(seg, False)
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
    if not failures:
        return None
    return (
        str(len(failures)) + ' audio segment(s) could not be deselected after the extract (first: '
        + failures[0] + ')'
    )


def _ts_shorten(seq, duration, library_name, reel_name):
    out, places, end = _ts_inspect(seq, duration)
    if out['error'] is not None:
        return out
    fp_before = _tl_fingerprint(seq)['fingerprint']
    audio_before = _tb_audio_read(seq)
    rows_before = {}
    for place in places:
        if place['media'] == 'video':
            rows_before[('video', place['track'], None)] = [_ts_vrow(s) for s in place['segs']]
    out.update(
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
    selected_before = _try(lambda: len(_td_members(seq.selected_segments)))
    message = _ts_cut_all(seq, places, end)
    if message is None:
        missed = _ts_uncut(seq, places, end)
        if missed:
            message = ('the cut at ' + str(end) + ' did not split ' + ', '.join(missed)
                       + ' (no segment starts at ' + str(end) + ' there)')
    if message is not None:
        return _ts_fail_after_cuts(out, seq, fp_before, audio_before, 'FLAME_API_ERROR', message)
    places = _ts_places(seq)
    targets = [(p, s) for p in places for s in p['segs'] if s.record_in.frame >= end]
    entries = _td_snapshot(seq)
    if selected_before is not None and selected_before != len(entries):
        out['warnings'].append(
            str(selected_before) + ' segment(s) were selected before the cuts and '
            + str(len(entries)) + ' after them: the selection put back is the one after the cuts'
        )
    try:
        lib, reel, out['libraryCreated'], out['reelCreated'] = _tl_stash_reel(
            library_name, reel_name)
    except BaseException as exc:
        return _ts_fail_after_cuts(
            out, seq, fp_before, audio_before,
            getattr(exc, 'code', 'FLAME_API_ERROR'),
            'the stash reel could not be made after the cuts at ' + str(end) + ': '
            + getattr(exc, 'message', type(exc).__name__ + ': ' + str(exc)),
        )
    out['stash'] = {'library': _try(lambda: _v(lib.name)), 'reel': _try(lambda: _v(reel.name))}
    dest_entry = _dc_with_selector(reel)
    before_uids = [_dm_uid(o) for o in _dm_media_children(reel)]
    shown_before = _tl_shown()
    returned = None
    problem = None
    what = 'selecting the segments'
    try:
        problem = _ts_select(seq, targets, entries)
        if problem is None:
            out['ran'] = True
            what = 'extract_selection_to_media_panel()'
            returned = seq.extract_selection_to_media_panel(reel)
    except BaseException as exc:
        out['exception'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        out['display'] = _tl_copy_display(shown_before)
        note = _ts_deselect_audio(targets)
        if note is not None:
            out['warnings'].append(note)
        kept = entries
        try:
            if _ts_tail_gone(seq, end):
                kept = [e for e in entries if e['in'] < end]
        except BaseException as exc:
            out['warnings'].append(
                'whether the extract took the selected segments at or after ' + str(end)
                + ' could not be read (' + type(exc).__name__ + ': ' + str(exc)
                + '): all of the selection is put back'
            )
        out['selection'] = _td_restore_selection(seq, kept, 0, 0, [], False)
        out['selection']['before'] = len(entries)
        out['selection']['dropped'] = len(entries) - len(kept)
        out['selection']['beforeCuts'] = selected_before
        if out['selection']['error'] is not None:
            out['warnings'].append(
                'the selection the segments had before could not be put back completely: '
                + out['selection']['error']
            )
    video_changed = _tl_fingerprint(seq)['fingerprint'] != fp_before
    audio_changed = _tb_audio_judge(out, audio_before, seq)
    out['changed'] = video_changed or audio_changed
    out['after'] = _ts_state(seq)
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
    if problem is not None:
        out['ran'] = False
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            problem + ': nothing was removed (the cuts at ' + str(end) + ' were made: the sequence '
            + ('CHANGED' if out['changed'] else 'is unchanged') + ')',
        )
    if out['exception'] is not None:
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            what + ' raised ' + out['exception'] + ' (the sequence '
            + ('CHANGED: read result.after and result.audio' if out['changed']
               else 'is unchanged') + ')',
        )
    if not out['changed']:
        return _tb_fail(
            out,
            'SILENT_NOOP',
            'extract_selection_to_media_panel() returned ' + repr(out['returned']['shape'])
            + ' and nothing in the sequence changed',
        )
    _tb_audio_only(out, video_changed, audio_changed)
    problems = _ts_judge(seq, places, rows_before, audio_before, end, duration)
    if problems:
        return _tb_fail(
            out,
            'FLAME_API_ERROR',
            'the sequence changed, but not the way the shortening is measured to change it: '
            + '; '.join(problems),
        )
    return out
"""
