# Native memory slice validation receipt

Base: `a64c88ce1bd29a830886e1afa9a828890059cfff`.
Date: 2026-11-13. All runtime fixtures used temporary profiles and synthetic
content. No provider/model/network experiment, live profile read/write, image
build, registry publication or deployment was performed.

Initial primary checkout status was clean. Measured tracked base content was
158,742,351 bytes; `storage-policy --need-gib 1 gate` returned `ok`, exit 0.
Worktree: `work/a78-native-memory`, isolated at the exact reviewed base.

## Red evidence

New behavior tests were written before their corresponding increments. The
initial scaffold failed collection on the missing revert API; that is not
counted as behavioral evidence. Subsequent executed failures were:

| Increment | Observed failure | Result |
| --- | --- | --- |
| Journey undo | CLI parser rejected `history`; deleted-item receipt could not restore | 15 passed, 2 failed; exit 1 |
| Both host dispatchers | Missing native provenance on worker route; integration context/peer setup needed correction | 18 passed, 4 failed; exit 1 |
| Copied-profile isolation | A copied database accepted the source profile's UUID handle | 22 passed, 1 failed; exit 1 |
| Deferred approval | Applying staged single/batch writes lost the original host causal context | 23 passed, 2 failed; exit 1 |
| Shared-store target concurrency | Barrier-driven MEMORY/USER edits collided through draft instance-global transaction snapshots | 25 passed, 1 failed; exit 1 |

The final implementation scopes handles to the active profile, shares host
context through the existing provenance owner and approval payload, and keeps
transaction snapshots separate for each target under the existing native lock.

## Green evidence

Each command used `HERMES_PYTHON=/home/brian/code2/hermes-agent-context-repair/.venv/bin/python`
and the required `scripts/run_tests.sh` runner with `--file-retries 0 -q -j 2`.
The environment came from an existing uv-managed test environment; no installation
or dependency change was needed.

| Paths / selection | Count | Exit | Measured wall time |
| --- | ---: | ---: | ---: |
| `tests/tools/test_native_memory_revisions.py`, `tests/tools/test_memory_tool.py`, `tests/agent/test_learning_graph.py`, `tests/agent/test_learning_mutations.py`, `tests/tools/test_write_approval.py` | 175 passed | 0 | 19.6 s |
| `tests/tools/test_memory_tool_import_fallback.py`, `tests/tools/test_memory_tool_schema.py`, `tests/tools/test_write_approval.py`, `tests/hermes_cli/test_journey_render.py`, `tests/agent/test_learning_graph_render.py`, `tests/test_hermes_state.py`, `tests/run_agent/test_background_review_cache_parity.py`, `tests/run_agent/test_background_review_summary.py`, `tests/run_agent/test_background_review.py` | 541 passed | 0 | 108.6 s |
| `tests/run_agent/test_run_agent.py -k memory` | 14 passed | 0 | 3.7 s |

These runs cover 696 distinct tests in 14 files (34 write-approval tests were
repeated in the final affected run). The new revision file contributes 26 tests.
Every failing test above was read before its fix; no file retry hid a failure.

`ruff check` over all 12 changed Python source/test files returned no issues,
exit 0. `git diff --check` returned no whitespace errors, exit 0.
`openspec validate --specs --strict` passed 1 specification with 0 failures,
exit 0. It reports four informational long-requirement notes because the
requirements include their pinned source links. The minimal change was archived
with `openspec archive native-memory-revisions --yes`, exit 0.

The existing repository has no `Makefile`, `docs/index.md` or `make docs-specs`
entry point at this base. No repository-format migration or Company Planning
compliance claim is part of this slice. The existing architecture page links the
behavior contract and usage documentation.

## Explicit limits

This is a local candidate for Coordinator review. It has not been pushed,
merged, published, deployed or activated. It covers native memory persistence and
existing journey consumers; it does not complete A78 skill mutation or enforce
policy against raw terminal/file writes. Existing scan, validation, approval and
prompt-cache owners remain in place. Historical provenance is never backfilled
from mtime or position. Revision retention is unbounded in this slice.

## Independent review corrections — 2026-11-13

The first candidate `5a919aa861186245402783325b89266d22753bb4` remained local.
Independent review reproduced four gaps despite the focused green suite:
reserved delimiters could produce a journal item count different from Markdown
reload; text-mode newline translation could diverge from the after hash and
existing CRLF files parsed incorrectly; initial frozen-prompt loading skipped
pending recovery; and redirected journey list output wrapped handles.

The correction first ran the new reproductions against that candidate:
`tests/tools/test_native_memory_revisions.py` returned **26 passed, 24 failed,
exit 1**, measured 19.4 seconds. This included 20 reserved-separator cases across
add/replace/batch/journey and four newline/snapshot/CLI seam cases. Subsequent
controls verify valid writes remain usable, exact CRLF-before/LF-after recovery
works at initial loading, cached conversations remain unchanged, and invalid
content cannot enter approval staging.

Final corrective commands used the same required runner, environment and
`--file-retries 0 -q -j 2` options above:

- Ten affected files (`test_native_memory_revisions`, `test_memory_tool`,
  `test_write_approval`, `test_learning_mutations`, `test_learning_graph`,
  `test_journey_render`, `test_background_review_cache_parity`,
  `test_background_review_summary`, `test_memory_tool_import_fallback`, and
  `test_memory_tool_schema`): **222 passed, exit 0**, measured 28.0 seconds.
- `tests/run_agent/test_run_agent.py -k memory`: **14 passed, exit 0**,
  measured 4.6 seconds.
- Ruff over the three corrective Python files: no issues, exit 0.
- `openspec validate --specs --strict`: 1 specification passed, exit 0.
- `git diff --check`: no whitespace errors, exit 0.

The revision test file now has 54 passing cases. These corrective runs cover
236 tests in 11 files. Earlier SessionDB and other nearby results remain
historical evidence; they are not represented as rerun after the correction.
Windows translation was simulated locally; no real Windows execution is claimed.

### Pre-landing coverage lesson

The earlier 696-test result accurately described tests executed against a local
candidate, not a full A78, shipping or activation certification. Independent
pre-landing review added decisive representation and entry-point controls:
compare persisted bytes, fresh Markdown parsing and journal identities; exercise
initial prompt loading as well as live readers; and round-trip the actual printed
handle into the next documented command. This note extends the existing receipt;
no separate verification/certification artifact or Project Meta mutation was added.
