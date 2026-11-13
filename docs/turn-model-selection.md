# Gateway turn model selection

The optional profile `config.yaml` section selects the effective model before
normal/background agent construction and normal agent-cache lookup. It makes no
classifier calls and never changes the session's `/model` setting.

```yaml
turn_model_selection:
  policy_revision: reality-a77-conservative-v1
  operation: unrestricted_turn
  reason: Source-answer quality evidence keeps unrestricted turns on Terra.
  model: openai/gpt-6.1-terra
  provider: openrouter
```

All five keys are required nonempty strings; additional keys are rejected.
The provider must equal the already resolved session provider. Credentials,
base URL, API mode and provider routing stay scoped to that runtime. Missing or
null configuration preserves existing resolution without adding a receipt.
Invalid configuration preserves the original model/runtime with an explicit
`invalid_policy` receipt. Removing the section disables selection next turn.

`unrestricted_turn` is the only accepted operation. Its provenance is `gateway`:
the gateway knows its native caller, but cannot establish a bounded task from
user prose, slash-command text, event metadata, or an instruction to use a
cheaper model. Future operation routing needs a real structured native caller
and its validation contract first.

Reasoning is resolved against the selected model. Service-tier and reasoning
values are turn-local in normal and background paths, so concurrent sessions
do not share mutable request settings. Fast-mode overrides use the selected
model. Same effective configuration reuses the cached agent; policy revision
and explanation alone do not invalidate it. A model change uses the existing
agent-cache transition. A cached fallback model cannot silently override a new
policy selection. Cache identity uses the same native-provider model-name
normalization as agent construction; the receipt retains the configured spelling.
After a successful turn, fallback detection compares against the selected target
instead of the profile's default model.

The existing `pre_api_request` hook carries `model_selection`: policy revision,
reason, operation/provenance, status, selected model/provider, and each request's
effective model/provider/reasoning/service tier. The Langfuse root and each
generation retain the receipt. Provider fallback requests have their own actual
model identity; the policy's selected model remains separately recorded. No
new telemetry is enabled: the existing opt-in observability plugin consumes
this hook only when enabled.

This conservative single-target policy is not evidence of model differentiation,
quality equivalence or savings. Reality A77 remains partial until its actual
consumer and full traces have been verified.

Source: Brian's Reality A77 per-answer model-selection request, relayed by the
coordinator on 2026-11-13; same-provider and unrestricted-operation boundaries
are implementation choices inferred from the native caller and credential scope.
