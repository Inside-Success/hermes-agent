# native-memory Specification

## Purpose
Keep native memory identity, causal provenance and recoverable edits consistent across existing Hermes consumers without changing Markdown prompts.

## Requirements

### Requirement: Stable revision-bound native memory handles

Native memory lists SHALL expose stable item identity and expected revision
identity. A mutation SHALL compare that revision under the same native file lock
used for materialization. Legacy positional, foreign-profile and stale handles
SHALL fail without mutating a different entry.

Source: Inferred by Coordinator as the reversible first native-memory slice of
Brian's approved A78 outcome, 2026-11-13; [accepted goal](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207)
and [A78 acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
This contract is a technical implementation slice, not full A78 completion.

#### Scenario: Earlier entry deleted

- **WHEN** entry A is deleted and the caller retries A's old handle
- **THEN** entry B remains unchanged and the stale request fails

#### Scenario: Concurrent edit

- **WHEN** two clients edit the same expected revision
- **THEN** exactly one succeeds and the other must refresh

#### Scenario: Foreign profile

- **WHEN** a caller presents a handle from another or copied profile
- **THEN** the native mutation fails without changing that profile's entries

### Requirement: Honest native provenance

New committed native writes SHALL record committed-write time separately from
observation time. Causal session, parent, source, chat and thread metadata SHALL
come from the active profile SessionDB, with task/tool-call/execution-origin
metadata supplied only by the host dispatcher. Historical creation time and
causal origin SHALL remain unknown when no committed native record proves them.

Source: Inferred by Coordinator as the reversible first native-memory slice of
Brian's approved A78 outcome, 2026-11-13; [accepted goal](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207)
and [A78 acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
This contract is a technical implementation slice, not full A78 completion.

#### Scenario: Foreground and background writes

- **WHEN** either existing native dispatcher saves a memory
- **THEN** its trusted native session and execution context are retained

#### Scenario: Historical duplicate entries

- **WHEN** existing manual entries are first observed
- **THEN** each has an independent item identity and unknown creation origin/time

#### Scenario: Deferred approval

- **WHEN** an existing approval owner applies a staged native write
- **THEN** the original host context is retained and validated in its profile

### Requirement: Recoverable file and provenance commit

MemoryStore SHALL prepare a journal entry in the existing profile SessionDB
before materializing Markdown, mark materialized, and commit provenance before
reporting success. Recovery SHALL accept only an exact before/after content hash;
any other file content SHALL block recovery and subsequent writes. Journal
unavailability SHALL prevent an acknowledged native write.

Source: Inferred by Coordinator as the reversible first native-memory slice of
Brian's approved A78 outcome, 2026-11-13; [accepted goal](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207)
and [A78 acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
This contract is a technical implementation slice, not full A78 completion.

#### Scenario: Failure around materialization

- **WHEN** failure leaves the file at the exact before or after image
- **THEN** recovery aborts an unwritten prepare or commits the materialized image

#### Scenario: Conflicting external content

- **WHEN** a pending journal sees neither exact image
- **THEN** recovery fails and leaves the external content unchanged

#### Scenario: Unavailable journal

- **WHEN** the native journal cannot prepare a durable write
- **THEN** no memory file change is made or acknowledged

### Requirement: Compensating journey revert

The existing journey CLI SHALL expose memory history and revision-bound revert.
Revert SHALL create a new compensating revision for one item, including restoration
from an exact deletion receipt, and SHALL preserve unrelated entries and reject
newer changes to that item. Native scan, validation and character-limit owners
SHALL remain in force. Prompt snapshots SHALL remain frozen for the conversation.

Source: Inferred by Coordinator as the reversible first native-memory slice of
Brian's approved A78 outcome, 2026-11-13; [accepted goal](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/plans/reality-brain-goal.md#L207)
and [A78 acceptance lineage](https://github.com/Inside-Success/Team-Brains/blob/bd76f8dc38d50790ef08060574c9a6c778231bb5/docs/planning/reality/acceptance-criteria.json#L1545).
This contract is a technical implementation slice, not full A78 completion.

#### Scenario: Revert after unrelated write

- **WHEN** A changes, B is added, and the caller reverts A's expected revision
- **THEN** A gets a new revision containing the selected prior content and B stays

#### Scenario: Preserve frozen prompt

- **WHEN** native files and provenance change during a conversation
- **THEN** the loaded system-prompt snapshot remains byte-identical
