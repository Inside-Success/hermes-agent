# Native memory revisions

Native `MEMORY.md` and `USER.md` remain §-delimited Markdown files. The existing
profile `state.db` now records item identity, revisions, before/after images and
causal metadata. Nothing changes the system-prompt memory snapshot after it has
been loaded for a conversation.

This is a bounded technical implementation of the [approved A78 outcome](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207),
with [acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
Source: Brian's accepted outcome, 2026-11-13; revision/journal design inferred by
Coordinator. This slice does not complete A78: skills, raw terminal/file mutation
policy enforcement, registry publication and live activation remain separate.

## Inspect, edit and undo

`hermes journey list` returns revision-bound memory handles. Each handle includes
the profile scope, stable item UUID and expected revision UUID. `item_id` in the
JSON graph remains stable across edits; `revision` changes on a committed edit.
Handles from another profile, including a copied profile database, are rejected.
Old positional IDs such as `memory:memory:0` cannot authorize a write.

```text
hermes journey list
hermes journey history <current-memory-handle>
hermes journey edit <current-memory-handle>
hermes journey revert <current-memory-handle> <prior-revision>
hermes journey delete <current-memory-handle>
```

History prints JSON with full content, causal metadata and revisions. Delete
prints a deletion receipt handle; pass that handle to `history` or `revert` to
inspect or restore the deleted item. Restore appends the item and preserves all
other current entries. Revert creates a new compensating revision; it never rolls
back the whole file or silently replaces a newer revision of the same item.
Refresh the list after an edit before making another change.

## Time and provenance

For new native writes, `timestamp` is the time the native file/provenance commit
was acknowledged (seconds since the Unix epoch). After crash recovery it records
the recovery commit, not an invented earlier completion time. `observed_at` is
separate. Existing manual/historical entries have unknown creation/write time
(`timestamp: null`) and unknown causal provenance (`provenance: null`). No mtime
or positional timestamp is presented as creation evidence.

Both existing native dispatchers pass host-only write context. Session, parent,
source, chat and thread values are resolved from the active profile SessionDB;
model arguments cannot supply them. Task ID, tool-call ID, write origin and
execution context come from the host dispatcher. Approval staging retains that
host context and its profile scope until the existing approval owner applies it. Background review retains its
existing shared parent-session/cache/persistence isolation; its execution origin
is recorded separately. Missing native peer fields remain unknown. Direct
sessionless native edits also have unknown causal provenance.

## Locking, recovery and limits

All native writes prepare a SessionDB journal entry while holding the existing
MemoryStore file lock. Transaction snapshots are isolated per target even when
the foreground and review fork share a MemoryStore. The file is materialized, the journal is marked
`materialized`, then the document snapshot and journal become `committed` in one
SQLite transaction. Native journal connections use SQLite `synchronous=FULL`;
file writes fsync their data and, where supported, the rename's directory before
provenance is committed. A failure never returns an acknowledged success with
unresolved provenance.

On the next native read/write, a pending prepare is resolved only when the exact
file hash matches its before or after image. Before means the unwritten prepare
is aborted; after means the materialized change is committed. Any third image
blocks reads/writes with a recovery conflict and leaves the file unchanged.
The journal retains both images for inspection; there is no automatic conflict
overwrite or data deletion. An unavailable journal prevents a native write.

An external edit without a pending write is observed conservatively. Unchanged
unique entries retain their identities; ambiguous duplicate matches get fresh
identities and unknown provenance. Native writes preserve duplicate manual
entries. Existing scanning, character limits, write approval and frozen prompt
owners remain in place. This journal adds no learning notices and contacts no
external provider.

These guarantees cover cooperating native writers using MemoryStore. Arbitrary
terminal/file writes do not acquire this lock and can still race native writes;
this slice does not claim action-owner enforcement against those paths. No
retention or pruning policy is added for revision history.

## Verification

`tests/tools/test_native_memory_revisions.py` uses temporary profiles and fake
host contexts to cover stale handles, duplicate/manual observation, concurrent
revision edits, profile-copy isolation, trusted foreground/background dispatch,
pre/post-materialization failures, commit failure, exact-hash conflict blocking,
unavailable journaling, frozen prompts, compensating restore and real journey
CLI dispatch. Nearby existing native memory, write-approval, session and cache
regressions remain part of the focused check.
