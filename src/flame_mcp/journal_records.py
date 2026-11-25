"""The shape of the journal's records (`journal.py` tells what each one is for).

One TypedDict per record `type`: `intent` and `outcome` (a write's pair, tied by `id`), `check`
(what `journal.check` found), `unresolved` (a write that ended without an answer), `pending` and
`polled` (a call that timed out, and each poll of its handle), `group` (a write group, a plan run
or a loop's parent; tied to the others by `group`), `checkpoint` (a stashed copy) and `purge` (a
`journal.purge_checkpoints` run). Every record has `type` and `ts`; all but `group` and `purge`
have `id`. A key that only one op writes (a render's `nodes`, a desktop write's `created`) is here
too, typed `Any`. A typo in a key of these types is a mypy error, and no key is renamed, so journal
files written before stay readable.
"""

from __future__ import annotations

from typing import Any, NotRequired, Required, TypedDict


class IntentRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    op: Required[str]
    path: str
    action: Any
    after: dict[str, Any]
    args: dict[str, Any]
    batch: Any
    before: dict[str, Any] | None
    betweenMarks: Any
    checkpoint: str | None
    checkpointPath: Any
    checkpoints: Any
    connectedBefore: Any
    content: Any
    createKind: str | None
    created: Any
    directoryBefore: Any
    duration: Any
    existing: Any
    expectedFrames: Any
    fingerprintBefore: str | None
    fingerprintExpected: str | None
    force: Any
    forced: dict[str, Any]
    frame: Any
    handle: str | None
    handleSource: str | None
    hiddenBefore: Any
    includeHandles: Any
    index: Any
    isRenderedBefore: Any
    kind: str
    marks: Any
    mode: Any
    name: Any
    nodeCount: Any
    nodeCountBefore: Any
    nodes: Any
    of: Any
    outputDir: Any
    pid: Any
    preCheckpoint: str
    preset: Any
    previousCurrent: Any
    readOp: str
    reelsBefore: Any
    renderMode: Any
    renderOption: Any
    renderQuality: Any
    requested: Any
    revertCheckpoint: str
    revertOf: str | None
    reverts: str
    revertsIntent: str
    runDir: Any
    segmentCountBefore: Any
    setupPath: Any
    shown: Any
    shownBefore: Any
    source: Any
    sources: Any
    stash: Any
    stashes: Any
    target: Any
    targetAfter: Any
    targetIdent: Any
    targetKind: str
    targetType: Any
    targetUid: str | None
    tempName: Any
    tempReel: Any
    tempReelPreexisting: Any
    timeoutS: Any
    uid: Any
    user: Any
    watchAttributes: Any
    code: str
    codeSha256: str
    plan: bool
    resolved: Any
    withoutBackup: Any


class OutcomeRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    applied: Any
    changed: Any
    changedKeys: Any
    countUnsure: Any
    created: Any
    deleted: Any
    displayedBefore: Any
    elapsedS: Any
    errorCode: Any
    expectedFrames: Any
    failed: Any
    fingerprintAfter: str | None
    fingerprintAfterError: Any
    isRenderedAfter: Any
    isRenderedBefore: Any
    matched: Any
    newClips: Any
    nodes: Any
    ok: Any
    reelsRestored: Any
    loadReturned: Any
    steps: Any
    stoppedAt: Any
    attributeFingerprintAfter: str | None
    notRestored: Any
    restored: Any
    reverted: Any
    preCheckpoint: str | None
    readBack: Any
    reopened: Any
    replacedUid: Any
    reshown: Any
    returned: Any
    setupPath: Any
    shownAfter: Any
    shownBefore: Any
    skipped: Any
    targetIdent: Any
    targetUid: str | None
    tempReelCreated: Any
    tempRemoved: Any
    touched: Any
    verdict: str | None
    warnings: Any
    writtenCount: Any
    writtenFrames: Any


class CheckRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    basis: str
    created: Any
    current: Any
    exportOutcome: Any
    flame: Any
    ok: Any
    perKey: Any
    renderOutcome: Any
    result: Any
    stillOutcome: Any
    target: Any
    verdict: str


class UnresolvedRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    error: Any
    errorCode: Any


class PendingRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    handle: str
    timeoutS: Any


class PolledRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: Required[str]
    ts: Required[str]
    error: Any
    errorCode: Any
    flame: Any
    handle: str
    ok: Any
    queuedMs: Any
    runMs: Any


class GroupRunFields(TypedDict, total=False):
    """The keys of the `group` record of a plan run or a loop's run of items."""

    label: Any
    steps: Any


class GroupFields(GroupRunFields, total=False):
    """The keys of a `group` record beside `type`, `group`, `ts` and `kind` (what `append_group`
    takes as keywords; each kind of group writes its own)."""

    before: Any
    parent: str
    key: Any
    op: str
    step: str
    revertible: Any
    keys: Any
    mode: str
    approval: str | None
    onError: Any
    late: Any


class GroupRecord(GroupFields):
    type: Required[str]
    group: Required[str]
    ts: Required[str]
    kind: NotRequired[str]


class CheckpointRecord(TypedDict, total=False):
    type: Required[str]
    id: Required[str]
    group: str | None
    ts: Required[str]
    kind: str
    label: Any
    batch: Any
    path: Any
    size: Any
    source: Any
    stash: Any
    fingerprint: Any
    fingerprintSections: Any
    fingerprintFull: Any
    fingerprintFullSections: Any
    sectionData: Any
    nodeCount: Any
    connectionCount: Any
    unreadableNodes: Any
    reels: Any
    reelsError: Any
    clipFx: Any
    clipFxError: Any
    segmentCount: Any
    displayed: Any
    effectCount: Any
    display: Any
    by: str
    replacedUid: Any
    orphaned: bool


class PurgeRecord(TypedDict):
    type: str
    id: str
    ts: str
    olderThanDays: Any
    groups: Any
    library: Any
    purged: list[str]
    gone: list[str]
    skipped: Any
    notDeleted: list[str]
    reelsDeleted: Any
