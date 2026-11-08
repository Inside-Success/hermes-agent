# Langfuse Observability Plugin

This plugin ships bundled with Hermes but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

Pick one:

```bash
# Interactive: walks you through credentials + SDK install + enable
hermes tools  # → Langfuse Observability

# Manual
pip install langfuse
hermes plugins enable observability/langfuse
```

## Required credentials

Set these in `~/.hermes/.env` (or via `hermes tools`):

```bash
HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...
HERMES_LANGFUSE_SECRET_KEY=sk-lf-...
HERMES_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
hermes plugins list                 # observability/langfuse should show "enabled"
hermes chat -q "hello"              # then check Langfuse for a "Hermes turn" trace
```

## Optional tuning

```bash
HERMES_LANGFUSE_ENV=production       # environment tag
HERMES_LANGFUSE_RELEASE=v1.0.0       # release tag
HERMES_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
HERMES_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
HERMES_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
hermes plugins disable observability/langfuse
```

## Complete model source content (explicit opt-in)

Default traces are bounded previews. To retain complete available model source
content, add this to the active profile's `config.yaml` and restart its process:

```yaml
plugins:
  enabled:
    - observability/langfuse
  entries:
    observability/langfuse:
      full_content: true
```

This sends full prompts, answers, exposed reasoning, tool schemas, arguments and
results to the configured Langfuse project. Use a project whose access and
retention are appropriate for those contents. Credential fields, known secret
environment values, credential assignments in text/JSON, and URL credentials are
redacted before export; redaction is enforced even if log redaction is disabled.
This is credential filtering, not general personal-data removal.
Credential-named schema properties keep their definitions; credential defaults,
constants and examples are filtered as values. Encoded JSON string layers are
checked recursively, while non-secret strings retain their original formatting.

Full mode records separate generations for every **Hermes dispatch attempt**,
including failed attempts and retries, through `pre_api_attempt` and
`post_api_attempt`. It snapshots final dispatch kwargs after execution middleware
and provider preflight, then captures the returned SDK object or error. Available
usage and estimated cost follow the existing pricing path. Metadata identifies
`full_content: true`, `captured_scope: hermes_dispatch_attempt`, and `redacted: true`.

JSON model source content has no plugin message-count, string-length, container-size
or nesting-depth preview limits. Tool output also bypasses `read_file` preview
normalization. Original JSON strings are preserved except where credentials need
redacting. Unsupported non-JSON objects and cycles receive explicit omission
markers; bytes receive a base64 representation. Arbitrary metadata stays bounded.

The scope is source data available at Hermes dispatch, not serialized HTTP wire
bytes, SDK-internal HTTP retries, transport-only stream/timeout settings, or
reasoning a provider does not expose. Langfuse SDK media handling, sampling and
server/exporter limits still apply; verify stored content before relying on a
deployment's complete-capture claim. Keep sampling at 1.0 when every run is needed.
This mode requires a Hermes version with the attempt hooks. Default preview mode
continues to support older hook variants.
