"""Prelude source for `batch.delete` (`ops/batch_delete.py`, `batch_delete_run.py`).

Real Python source that `execution.build_exec_code` places ahead of an op's own code. It runs inside
Flame (3.11) with `flame`, `_v`, `_try`, `_ja`, `_OpError` and `_save_setup` (batch_prelude)
already defined. Three calls, each answering a dict (`error` / `errorCode` None when fine):

  `_bd_inspect(batch, allow_current, allow_loss, expect_name)`  reads the facts and judges them
  `_bd_save(...)`        the same, then saves the group's setup into `directory`
                         (`_save_setup`, never overwriting); no save, no delete
  `_bd_delete(...)`      the same judgement again, then deletes the group and looks its uid up

What real Flame may change is confined to these functions:
  - `_bd_facts(batch)`: current group, `PyBatch.opened`, the groups of the Desktop, the entries of
    the reels and iterations (a value that cannot be read is `None`, never a guess)
  - `_bd_blocked(facts, ...)`: which groups are not deleted
  - `_bd_delete_call(batch)`: the call that deletes a Batch group
"""

from __future__ import annotations

BATCH_DELETE_HELPER = """

def _bd_content(batch):
    # entries (clips and sequences) the group's reels hold, and its iterations; None = unreadable
    try:
        schematic = 0
        for reel in list(batch.reels):
            schematic += len(list(reel.clips)) + len(list(reel.sequences))
        shelf = 0
        for reel in list(batch.shelf_reels):
            shelf += len(list(reel.clips)) + len(list(reel.sequences))
        return {
            'schematicReelEntries': schematic, 'shelfReelEntries': shelf,
            'iterations': len(list(batch.batch_iterations)),
        }
    except BaseException:
        return None


def _bd_facts(batch):
    desktop = flame.projects.current_project.current_workspace.desktop
    groups = list(desktop.batch_groups)
    facts = {
        'name': _try(lambda: _v(batch.name)), 'uid': _try(lambda: _v(batch.uid)),
        'groupUids': [_try(lambda g=g: _v(g.uid)) for g in groups],
        'currentUid': None, 'currentReadable': True,
        'opened': None, 'openedReadable': True,
        'nodeCount': _try(lambda: len(list(batch.nodes))),
        'content': _bd_content(batch),
    }
    try:
        current = _v(desktop.current_batch_group)
        facts['currentUid'] = _v(current.uid) if current is not None else None
    except BaseException:
        facts['currentReadable'] = False
    try:
        opened = _v(batch.opened)
        if not isinstance(opened, bool):
            raise ValueError('opened is not a bool')
        facts['opened'] = opened
    except BaseException:
        facts['openedReadable'] = False
    facts['wasCurrent'] = facts['uid'] is not None and facts['uid'] == facts['currentUid']
    return facts


def _bd_blocked(facts, allow_current, allow_loss, expect_name):
    # (code, message) of a group that is not deleted, else None
    if facts['uid'] is None or facts['uid'] not in facts['groupUids']:
        return ('PRECONDITION_UNMET', 'the group is not a Batch group of the Desktop')
    if expect_name is not None and facts['name'] != expect_name:
        return ('CONFLICT', 'the group is named ' + repr(facts['name'])
                + ', not ' + repr(expect_name) + ': nothing was deleted')
    if len(facts['groupUids']) < 2:
        return ('PRECONDITION_UNMET', 'the only Batch group of the Desktop is not deleted')
    if not allow_current:
        if not facts['currentReadable'] or not facts['openedReadable']:
            return ('PRECONDITION_UNMET', 'it could not be read whether the group is the current '
                    'or an open one (allow_current deletes it anyway)')
        if facts['wasCurrent']:
            return ('PRECONDITION_UNMET', 'the group is the current Batch group (allow_current)')
        if facts['opened']:
            return ('PRECONDITION_UNMET', 'the group is open (allow_current)')
    if not allow_loss:
        content = facts['content']
        if content is None:
            return ('PRECONDITION_UNMET', 'what the group\\'s reels and iterations hold could not '
                    'be read (allow_content_loss deletes it anyway)')
        if content['schematicReelEntries'] or content['shelfReelEntries'] or content['iterations']:
            return ('PRECONDITION_UNMET', 'the group holds ' + repr(content) + ', which a saved '
                    'setup does not bring back (allow_content_loss deletes it anyway)')
    return None


def _bd_delete_call(batch):
    return flame.delete(batch, confirm=False)


def _bd_inspect(batch, allow_current, allow_loss, expect_name):
    out = _bd_facts(batch)
    out.update(setupPath=None, setupSize=None, deleted=False, flameReturned=None,
               warnings=[], error=None, errorCode=None)
    blocked = _bd_blocked(out, allow_current, allow_loss, expect_name)
    if blocked is not None:
        out['errorCode'], out['error'] = blocked
    return out


def _bd_save(batch, directory, allow_current, allow_loss, expect_name):
    import os
    import time
    out = _bd_inspect(batch, allow_current, allow_loss, expect_name)
    if out['error'] is not None:
        return out
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # the whole uid: its head is the same for every group made in one Flame session
    stem = 'bd_' + time.strftime('%y%m%d_%H%M%S') + '_' + ''.join(
        c for c in str(out['uid']) if c.isalnum())
    saved = _save_setup(batch, os.path.join(directory, stem), False)
    out['setupPath'], out['setupSize'] = saved['path'], saved['size']
    if saved['error'] is not None or not saved['path'] or not saved['size']:
        out['errorCode'] = 'FLAME_API_ERROR'
        out['error'] = (
            'the setup could not be saved, so nothing was deleted: ' + str(saved['error'])
        )
    return out


def _bd_delete(batch, allow_current, allow_loss, expect_name, setup_path):
    out = _bd_inspect(batch, allow_current, allow_loss, expect_name)
    out['setupPath'] = setup_path
    if out['error'] is not None:
        return out
    raised = None
    try:
        out['flameReturned'] = _ja(_bd_delete_call(batch))
    except BaseException as exc:
        raised = 'delete raised ' + type(exc).__name__ + ': ' + str(exc)
    desktop = flame.projects.current_project.current_workspace.desktop
    remaining = [_try(lambda g=g: _v(g.uid)) for g in list(desktop.batch_groups)]
    out['deleted'] = out['uid'] not in remaining
    if out['deleted']:
        if raised is not None:
            out['warnings'].append(raised + ' (the group is gone)')
    elif raised is not None:
        out['errorCode'], out['error'] = 'FLAME_API_ERROR', raised
    else:
        out['errorCode'] = 'SILENT_NOOP'
        out['error'] = (
            'the delete returned ' + repr(out['flameReturned']) + ' but the group is still there'
        )
    return out


def _bd_alive(uid):
    desktop = flame.projects.current_project.current_workspace.desktop
    uids = [_try(lambda g=g: _v(g.uid)) for g in list(desktop.batch_groups)]
    return {'uid': uid, 'there': uid in uids, 'groups': len(uids)}
"""
