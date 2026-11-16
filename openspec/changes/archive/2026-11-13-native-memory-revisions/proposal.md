# Native memory revision safety

## Why

Journey memory IDs used a file position, so deleting an earlier entry could
make a stale handle modify another entry. File mtime was presented as memory
creation time, and native writes did not persist their causal metadata.

Source: Inferred by Coordinator as the reversible first native-memory slice of
Brian's approved A78 outcome, 2026-11-13; [accepted goal](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207)
and [A78 acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
This contract is a technical implementation slice, not full A78 completion.
Registry publication and live activation are explicitly deferred.

## What Changes

- Extend profile SessionDB with native memory snapshots and a recoverable journal.
- Preserve the Markdown parser and frozen prompt snapshot.
- Carry trusted execution context through both existing native dispatchers.
- Bind existing journey handles to stable item and expected revision identities.
- Expose history and compensating revert through the existing journey CLI.

## Impact

Only native memory persistence and its existing consumers change. Legacy positional
handles cannot authorize writes. Skill mutations, policy enforcement against raw
terminal writes, registry publication, deployments, and live profile changes remain
outside this slice. No model, provider, network or live memory experiments are needed.
