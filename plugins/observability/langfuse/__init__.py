"""langfuse — Hermes plugin for Langfuse observability.

Traces Hermes conversations, LLM calls, and tool usage to Langfuse.

Activation is handled by the Hermes plugin system — standalone plugins only
load when listed in ``plugins.enabled`` (via ``hermes plugins enable
observability/langfuse`` or ``hermes tools → Langfuse Observability``). At
runtime the plugin also requires the ``langfuse`` SDK and credentials; if
either is missing the hooks are inert.

Required env vars (set via ``hermes tools`` or ~/.hermes/.env):
  HERMES_LANGFUSE_PUBLIC_KEY  - Langfuse project public key (pk-lf-...)
  HERMES_LANGFUSE_SECRET_KEY  - Langfuse project secret key (sk-lf-...)
  HERMES_LANGFUSE_BASE_URL    - Langfuse server URL (default: https://cloud.langfuse.com)

Optional env vars:
  HERMES_LANGFUSE_ENV         - environment tag (e.g. "production", "local")
  HERMES_LANGFUSE_RELEASE     - release/version tag
  HERMES_LANGFUSE_SAMPLE_RATE - sampling rate 0.0–1.0 (default: 1.0)
  HERMES_LANGFUSE_MAX_CHARS   - max chars per field (default: 12000)
  HERMES_LANGFUSE_DEBUG       - set to "true" for verbose logging
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse, propagate_attributes
except Exception:  # pragma: no cover - fail-open when optional dep is missing
    Langfuse = None
    propagate_attributes = None


@dataclass
class TraceState:
    trace_id: str
    root_ctx: Any
    root_span: Any
    generations: Dict[str, Any] = field(default_factory=dict)
    tools: Dict[str, Any] = field(default_factory=dict)
    pending_tools_by_name: Dict[str, list] = field(default_factory=dict)
    turn_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    last_updated_at: float = field(default_factory=time.time)


_STATE_LOCK = threading.Lock()
_TRACE_STATE: Dict[str, TraceState] = {}
# Hard cap on live trace state. Each turn keys _TRACE_STATE by a unique
# turn_id, and an entry is normally reclaimed by _finish_trace when a turn
# ends cleanly (final response has content and no tool calls). A turn that
# never reaches that state — interrupted, a tool-only final step, or empty
# final content — would otherwise linger forever, so over the cap we evict
# the least-recently-updated entries (ending their root span first). The cap
# is far above any realistic concurrent-live-turn working set; it exists only
# to bound the leak from non-finalizing turns, not to limit concurrency.
_MAX_TRACE_STATE = 256
_LANGFUSE_CLIENT = None
_READ_FILE_LINE_RE = re.compile(r"^\s*(\d+)\|(.*)$")
_READ_FILE_HEAD_LINES = 25
_READ_FILE_TAIL_LINES = 15

# Credential syntax, not prose classification. Also catches opaque run tokens
# embedded in instructions and JSON-encoded tool arguments.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(\b(?:[a-z0-9]+[_-])*(?:api[ _-]?key|secret[ _-]?key|access[ _-]?key(?:[ _-]?id)?|"
    r"private[ _-]?key|run[ _-]?tokens?|access[ _-]?token|"
    r"refresh[ _-]?token|token|secret|password|passwd|credential|authorization|cookie)"
    r"\b[\"']?\s*[:=]\s*)(?:Bearer\s+|Basic\s+|Token\s+)?"
    r'''("(?:\\.|[^"\\])*"|'[^']*'|[^\s,;"'}\]]+)''',
    re.IGNORECASE,
)


def _sensitive_content_key(key: Any) -> bool:
    # Accept standard snake/kebab/camel credential names without classifying
    # token counts, usage buckets, or arbitrary fields containing "key".
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).casefold().replace("-", "_")
    return name in {
        "api_key", "apikey", "authorization", "proxy_authorization", "cookie", "set_cookie",
        "password", "passwd", "secret", "token", "run_tokens", "private_key",
        "credential", "credentials", "auth", "bearer", "secret_value", "raw_secret",
        "secret_input", "key_material", "secret_key", "access_key", "access_key_id",
    } or name.endswith(("_api_key", "_token", "_secret", "_password", "_passwd", "_credential",
                         "_credentials", "_private_key", "_secret_key", "_access_key", "_access_key_id"))


_SCHEMA_ROOT_KEYS = {"parameters", "input_schema", "json_schema", "schema"}
_SCHEMA_MAP_KEYS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
                    "dependencies", "dependentRequired"}
_SCHEMA_NODE_KEYS = {"items", "additionalItems", "additionalProperties", "unevaluatedItems",
                     "unevaluatedProperties", "contains", "not", "if", "then", "else", "propertyNames",
                     "anyOf", "oneOf", "allOf", "prefixItems"}
_SCHEMA_MARKERS = {"type", "$ref", "$dynamicRef"} | _SCHEMA_MAP_KEYS | _SCHEMA_NODE_KEYS
_SCHEMA_VALUE_KEYS = {"default", "const", "enum", "examples", "example"}


def _full_content_enabled() -> bool:
    """Resolve capture policy in the current profile context, never at registration."""
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active
        from hermes_constants import get_hermes_home_override
        from hermes_cli.config import load_config_readonly

        if is_multiplex_active() and (current_secret_scope() is None or not get_hermes_home_override()):
            return False
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
        return (entries.get("observability/langfuse") or {}).get("full_content", False) is True
    except Exception:
        return False


def _full_value(value: Any) -> Any:
    """Copy complete JSON model source content, redacting credentials first.

    There are no message/string/container/depth preview limits. Non-JSON types
    have explicit representations; cycles/unsupported types are marked, never
    silently presented as complete content. This does not inspect HTTP wire
    bytes or recover reasoning a provider did not expose.
    """
    from agent.redact import redact_sensitive_text

    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active
        scope = current_secret_scope()
        if is_multiplex_active() and scope is None:
            return {"omitted": True, "reason": "profile secrets unavailable"}
        scoped_values = set()
        if scope is not None:
            for scoped_value in scope.values():
                if not isinstance(scoped_value, str):
                    return {"omitted": True, "reason": "profile secrets unavailable"}
                if scoped_value:
                    scoped_values.add(scoped_value)
        secrets = sorted(scoped_values | {v for k, v in os.environ.items()
                         if v and _sensitive_content_key(k)}, key=len, reverse=True)
    except Exception:
        return {"omitted": True, "reason": "profile secrets unavailable"}
    active: set[int] = set()

    def text_value(text: str) -> str:
        # Tool arguments/results can themselves contain JSON strings, including
        # further JSON-encoded strings. Preserve original bytes when unchanged;
        # parse recursively only to remove credential fields at every level.
        if text.lstrip().startswith(("{", "[", '"')):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                pass
            else:
                cleaned = copy(parsed)
                if cleaned != parsed:
                    return json.dumps(cleaned, ensure_ascii=False)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _SECRET_ASSIGNMENT_RE.sub(
            lambda m: m.group(1) + ('"[REDACTED]"' if m.group(2).startswith('"')
                                    else "[REDACTED]"), text,
        )
        return redact_sensitive_text(text, force=True, redact_url_credentials=True)

    def copy(item: Any, *, schema: bool = False, schema_map: bool = False,
             credential_schema: bool = False) -> Any:
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else {"type": "float", "value": str(item)}
        if isinstance(item, str):
            return text_value(item)
        if isinstance(item, (bytes, bytearray)):
            # Binary data can encode credentials in arbitrary forms, so neither
            # text redaction nor a base64 wrapper makes it safe to export.
            return {"type": "bytes", "omitted": True,
                    "reason": "opaque binary may contain credentials", "length": len(item)}
        ident = id(item)
        if ident in active:
            return {"omitted": True, "reason": "cyclic non-JSON value", "type": type(item).__name__}
        active.add(ident)
        try:
            if isinstance(item, dict):
                # JSON Schema property names describe future values; they are
                # part of the tool contract. Their defaults/examples are DATA,
                # so a schema context must never exempt them from redaction.
                result = {}
                # A Python mapping key can be bytes, a tuple containing bytes,
                # or an arbitrary object's repr. Never stringify those keys:
                # doing so can disclose both the key and its sensitive value.
                # Reserve the cleaned text keys so omission markers cannot
                # overwrite even a later literal key with the same name.
                reserved = {text_value(key) for key in item if isinstance(key, str)}

                def unique_key(base: str) -> str:
                    name = base
                    suffix = 1
                    while name in result or (name != base and name in reserved):
                        name = f"{base} [{suffix}]"
                        suffix += 1
                    return name

                for key, val in item.items():
                    if not isinstance(key, str):
                        base = "[omitted non-string key]"
                        safe_key = base
                        suffix = 1
                        while safe_key in reserved or safe_key in result:
                            safe_key = f"{base} [{suffix}]"
                            suffix += 1
                        result[safe_key] = "[OMITTED: non-string mapping key]"
                        continue
                    safe_key = unique_key(text_value(key))
                    if schema_map:
                        safe_val = copy(val, schema=True, credential_schema=_sensitive_content_key(key))
                    elif schema:
                        if key in _SCHEMA_VALUE_KEYS:
                            safe_val = "[REDACTED]" if credential_schema else copy(val)
                        elif key in _SCHEMA_MAP_KEYS:
                            safe_val = copy(val, schema_map=True)
                        elif key in _SCHEMA_NODE_KEYS:
                            safe_val = copy(val, schema=True, credential_schema=credential_schema)
                        elif _sensitive_content_key(key):
                            safe_val = "[REDACTED]"
                        else:
                            safe_val = copy(val)
                    elif _sensitive_content_key(key):
                        safe_val = "[REDACTED]"
                    else:
                        is_schema = key in _SCHEMA_ROOT_KEYS and isinstance(val, dict) and bool(_SCHEMA_MARKERS & val.keys())
                        safe_val = copy(val, schema=is_schema)
                    result[safe_key] = safe_val
                return result
            if isinstance(item, (list, tuple, set)):
                return [copy(v, schema=schema, credential_schema=credential_schema) for v in item]
            if isinstance(item, BaseException):
                if all(isinstance(arg, (str, int, float, bool, type(None))) for arg in item.args):
                    return {"type": type(item).__name__, "message": text_value(str(item))}
                # Exception repr embeds arbitrary arguments (including bytes).
                return {"type": type(item).__name__, "arguments": copy(item.args)}
            if callable(getattr(item, "model_dump", None)):
                return copy(item.model_dump(mode="python"))
            if hasattr(item, "__dict__"):
                return copy(vars(item))
            return {"omitted": True, "reason": "unsupported non-JSON value", "type": type(item).__name__}
        finally:
            active.remove(ident)

    return copy(value)


def _content_value(value: Any, **preview_kwargs: Any) -> Any:
    return _full_value(value) if _full_content_enabled() else _safe_value(value, **preview_kwargs)


def _metadata_value(value: Any) -> Any:
    return _safe_value(_full_value(value)) if _full_content_enabled() else value

# Langfuse-issued keys always carry these prefixes (cloud or self-hosted —
# the prefix is baked into the server-side issuance flow, not a UI hint).
# Anything else (`placeholder`, `test-key`, `your-langfuse-key`, etc.) is a
# leftover template value and would cause the SDK to silently accept the
# credentials at construction time but drop every trace at flush time.
# See #23823 — the silent-failure bug this guard fixes.
_LANGFUSE_KEY_PREFIXES: Dict[str, str] = {
    "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-",
    "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(*names: str) -> bool:
    for name in names:
        value = _env(name).lower()
        if value:
            return value in {"1", "true", "yes", "on"}
    return False


def _debug_enabled() -> bool:
    return _env_bool("HERMES_LANGFUSE_DEBUG")


def _debug(message: str) -> None:
    if _debug_enabled():
        logger.info("Langfuse tracing: %s", message)


# Sentinel: "_get_langfuse() has tried and failed". Lets us short-circuit
# every subsequent hook call without re-checking env vars or re-attempting
# SDK init. Tests clear this by reloading the module via
# ``sys.modules.pop(...) + importlib.import_module(...)`` rather than via a
# dedicated reset function. Runtime callers cannot reset the cache; if an
# operator fixes a misconfigured credential they must restart the process.
_INIT_FAILED = object()


def _redact_key_preview(value: str) -> str:
    """Return a brief, log-safe preview of a credential value.

    Keeps enough characters to disambiguate common placeholders
    (``placeholder``, ``test-key``, ``your-key``) without echoing a
    real secret in full if an operator pasted one into the wrong env
    var.  Used only for the once-per-process placeholder-detection
    warning in :func:`_get_langfuse`.
    """
    if not value:
        return "<empty>"
    if len(value) <= 12:
        return repr(value)
    return repr(value[:6] + "...")


def _validate_langfuse_key(env_name: str, value: str) -> Optional[str]:
    """Return an error message if ``value`` is not a real Langfuse key.

    Returns ``None`` when the value matches the documented Langfuse
    prefix for ``env_name``, or when no prefix is registered for the
    name (in which case we trust the operator).  When validation
    fails the returned string is suitable for direct inclusion in a
    single log line — it names the env var and shows a safe preview.
    """
    expected = _LANGFUSE_KEY_PREFIXES.get(env_name, "")
    if not expected:
        return None
    if value.startswith(expected):
        return None
    return (
        f"{env_name}={_redact_key_preview(value)} "
        f"(expected {expected!r} prefix)"
    )


def _get_langfuse() -> Optional[Langfuse]:
    """Return a cached Langfuse client, or ``None`` if unavailable.

    Activation of this plugin is controlled by the Hermes plugin system —
    this function only handles the runtime-availability gate (SDK installed
    + credentials present). The result is cached: on the first call we try
    to construct a client, and every subsequent call returns that client
    (or fast-returns ``None`` if init failed).
    """
    global _LANGFUSE_CLIENT
    if _LANGFUSE_CLIENT is _INIT_FAILED:
        return None
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT

    if Langfuse is None:
        _LANGFUSE_CLIENT = _INIT_FAILED
        return None

    public_key = _env("HERMES_LANGFUSE_PUBLIC_KEY") or _env("LANGFUSE_PUBLIC_KEY")
    secret_key = _env("HERMES_LANGFUSE_SECRET_KEY") or _env("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        _LANGFUSE_CLIENT = _INIT_FAILED
        return None

    # Reject placeholder credentials with a one-shot warning so the
    # operator sees the misconfiguration instead of silently shipping a
    # broken observability stack (#23823).  The SDK does not validate
    # keys at construction time — it queues traces in memory and only
    # discovers the auth failure when the background flush thread tries
    # to post them, by which point the warning is buried under whatever
    # else the process is logging.  Catch it here, surface it once, and
    # short-circuit via the same _INIT_FAILED path as the empty case.
    placeholder_issues = [
        msg
        for msg in (
            _validate_langfuse_key("HERMES_LANGFUSE_PUBLIC_KEY", public_key),
            _validate_langfuse_key("HERMES_LANGFUSE_SECRET_KEY", secret_key),
        )
        if msg
    ]
    if placeholder_issues:
        logger.warning(
            "Langfuse plugin: credentials look like placeholders, traces will "
            "NOT be emitted (%s). Set real Langfuse keys (pk-lf-... / sk-lf-...) "
            "or unset HERMES_LANGFUSE_PUBLIC_KEY / HERMES_LANGFUSE_SECRET_KEY to "
            "silence this warning.",
            "; ".join(placeholder_issues),
        )
        _LANGFUSE_CLIENT = _INIT_FAILED
        return None

    base_url = _env("HERMES_LANGFUSE_BASE_URL") or _env("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    environment = _env("HERMES_LANGFUSE_ENV") or _env("LANGFUSE_ENV")
    release = _env("HERMES_LANGFUSE_RELEASE") or _env("LANGFUSE_RELEASE")
    sample_rate = _env("HERMES_LANGFUSE_SAMPLE_RATE")

    kwargs: Dict[str, Any] = {
        "public_key": public_key,
        "secret_key": secret_key,
        "base_url": base_url,
    }
    if environment:
        kwargs["environment"] = environment
    if release:
        kwargs["release"] = release
    if sample_rate:
        try:
            kwargs["sample_rate"] = float(sample_rate)
        except ValueError:
            logger.warning("Invalid HERMES_LANGFUSE_SAMPLE_RATE=%r", sample_rate)

    try:
        _LANGFUSE_CLIENT = Langfuse(**kwargs)
    except Exception as exc:  # pragma: no cover - fail-open
        logger.warning("Could not initialize Langfuse client: %s", exc)
        _LANGFUSE_CLIENT = _INIT_FAILED
        return None

    return _LANGFUSE_CLIENT


def _scope_prefix(task_id: str, session_id: str) -> str:
    """The task/session/thread prefix shared by every trace-key shape."""
    if task_id:
        return f"task:{task_id}"
    if session_id:
        return f"session:{session_id}"
    return f"thread:{threading.get_ident()}"


def _trace_key(
    task_id: str,
    session_id: str,
    *,
    turn_id: str = "",
    api_request_id: str = "",
) -> str:
    """Build a stable in-process trace scope key for one agent turn.

    Older Hermes paths only expose ``task_id``/``session_id``. Newer paths
    pass ``turn_id`` and ``api_request_id`` in LLM/tool hooks; when present,
    they must scope trace state so concurrent requests sharing one task/session
    never collide. ``turn_id`` is preferred over ``api_request_id`` so the
    turn-level ``post_llm_call`` hook (which carries ``turn_id`` but no
    ``api_request_id``) resolves to the same key as the request-level hooks.
    """
    if turn_id:
        return f"{_scope_prefix(task_id, session_id)}:turn:{turn_id}"
    if api_request_id:
        return f"{_scope_prefix(task_id, session_id)}:api:{api_request_id}"
    # Legacy shape: a bare ``task_id`` (NOT the ``task:`` prefix) when present,
    # otherwise the session/thread prefix. Kept distinct for backward
    # compatibility with keys minted before turn/request scoping existed.
    if task_id:
        return task_id
    return _scope_prefix(task_id, session_id)


def _is_base64_data_uri(value: str) -> bool:
    prefix = value[:200].lower()
    return prefix.startswith("data:") and ";base64," in prefix


def _redact_data_uri(value: str) -> dict[str, Any]:
    header = value.split(",", 1)[0] if "," in value else "data:"
    media_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    return {
        "type": "data_uri",
        "media_type": media_type or None,
        "omitted": True,
        "length": len(value),
    }


def _truncate_text(value: str, max_chars: int) -> Any:
    # Langfuse SDK treats data:*;base64 strings as media and attempts to
    # decode them. Truncating those strings produces invalid base64 and noisy
    # "Error parsing base64 data URI" logs. Observability only needs metadata,
    # not raw image/audio payloads, so redact the whole data URI before it
    # reaches the SDK.
    if _is_base64_data_uri(value):
        return _redact_data_uri(value)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"


def _maybe_parse_json_string(value: str) -> Any:
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] not in "{[" or stripped[-1] not in "}]":
        if len(stripped) < 2 or stripped[0] not in "{[":
            return value
    try:
        parsed, idx = json.JSONDecoder().raw_decode(stripped)
    except Exception:
        return value
    if not isinstance(parsed, (dict, list)):
        return value

    trailing = stripped[idx:].strip()
    if not trailing:
        return parsed

    hint_key = "_hint" if trailing.startswith("[Hint:") else "_trailing_text"
    if isinstance(parsed, dict):
        merged = dict(parsed)
        key = hint_key if hint_key not in merged else "_trailing_text"
        merged[key] = trailing
        return merged

    return {"data": parsed, hint_key: trailing}


def _looks_like_read_file_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    content = value.get("content")
    return (
        isinstance(content, str)
        and "total_lines" in value
        and "file_size" in value
        and "is_binary" in value
        and "is_image" in value
        and not value.get("error")
    )


def _parse_read_file_lines(content: str) -> list[dict[str, Any]]:
    if not isinstance(content, str) or not content:
        return []

    lines = []
    for raw_line in content.splitlines():
        match = _READ_FILE_LINE_RE.match(raw_line)
        if not match:
            return []
        lines.append({
            "line": int(match.group(1)),
            "text": match.group(2),
        })
    return lines


def _build_read_file_preview(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if len(lines) <= (_READ_FILE_HEAD_LINES + _READ_FILE_TAIL_LINES):
        return {"lines": lines}

    return {
        "head": lines[:_READ_FILE_HEAD_LINES],
        "tail": lines[-_READ_FILE_TAIL_LINES:],
        "omitted_line_count": len(lines) - _READ_FILE_HEAD_LINES - _READ_FILE_TAIL_LINES,
    }


def _normalize_read_file_payload(value: dict[str, Any], *, args: Any = None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if isinstance(args, dict):
        path = args.get("path")
        offset = args.get("offset")
        limit = args.get("limit")
        if isinstance(path, str) and path:
            normalized["path"] = path
        if isinstance(offset, int):
            normalized["offset"] = offset
        if isinstance(limit, int):
            normalized["limit"] = limit

    lines = _parse_read_file_lines(value.get("content", ""))
    if lines:
        normalized["returned_lines"] = {
            "start": lines[0]["line"],
            "end": lines[-1]["line"],
            "count": len(lines),
        }
        normalized["content_preview"] = _build_read_file_preview(lines)
    elif value.get("content"):
        normalized["content_preview"] = {
            "text": value.get("content", ""),
        }

    for key in (
        "total_lines",
        "file_size",
        "truncated",
        "is_binary",
        "is_image",
        "hint",
        "_warning",
        "mime_type",
        "dimensions",
        "similar_files",
        "error",
    ):
        if key in value:
            normalized[key] = value[key]

    base64_content = value.get("base64_content")
    if isinstance(base64_content, str) and base64_content:
        normalized["base64_content"] = {
            "omitted": True,
            "length": len(base64_content),
        }

    return normalized


def _normalize_payload(value: Any, *, tool_name: str = "", args: Any = None) -> Any:
    if _looks_like_read_file_payload(value):
        return _normalize_read_file_payload(
            value,
            args=args if tool_name == "read_file" else None,
        )
    return value


def _safe_value(value: Any, *, max_chars: Optional[int] = None, depth: int = 0,
                parse_json_strings: bool = False) -> Any:
    max_chars = max_chars if max_chars is not None else int(_env("HERMES_LANGFUSE_MAX_CHARS", "12000") or "12000")
    if depth > 4:
        return "<max-depth>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "len": len(value)}
    if isinstance(value, str):
        if parse_json_strings:
            parsed = _maybe_parse_json_string(value)
            if parsed is not value:
                return _safe_value(parsed, max_chars=max_chars, depth=depth, parse_json_strings=True)
        return _truncate_text(value, max_chars)
    if isinstance(value, dict):
        normalized = _normalize_payload(value)
        if normalized is not value:
            return _safe_value(normalized, max_chars=max_chars, depth=depth, parse_json_strings=parse_json_strings)
        return {
            str(k): _safe_value(v, max_chars=max_chars, depth=depth + 1, parse_json_strings=parse_json_strings)
            for k, v in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _safe_value(v, max_chars=max_chars, depth=depth + 1, parse_json_strings=parse_json_strings)
            for v in list(value)[:50]
        ]
    if hasattr(value, "__dict__"):
        return _safe_value(vars(value), max_chars=max_chars, depth=depth + 1, parse_json_strings=parse_json_strings)
    return _truncate_text(repr(value), max_chars)


def _extract_last_user_message(messages: Any) -> Any:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return {
                "role": "user",
                "content": _content_value(message.get("content")),
            }
    return None


def _coerce_request_messages(
    *,
    request_messages: Any = None,
    messages: Any = None,
    conversation_history: Any = None,
    user_message: Any = None,
) -> list[dict[str, Any]]:
    for candidate in (request_messages, messages, conversation_history):
        if isinstance(candidate, list):
            return candidate
    if user_message is None:
        return []
    return [{"role": "user", "content": user_message}]


def _serialize_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    serialized = []
    for message in messages[-12:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        item = {
            "role": role,
            "content": _safe_value(
                message.get("content"),
                parse_json_strings=(role == "tool"),
            ),
        }
        if role == "tool":
            if message.get("tool_call_id"):
                item["tool_call_id"] = message.get("tool_call_id")
            if message.get("name"):
                item["name"] = _safe_value(message.get("name"))
        if message.get("tool_calls"):
            item["tool_calls"] = _safe_value(message.get("tool_calls"), parse_json_strings=True)
        serialized.append(item)
    return serialized


def _serialize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not tool_calls:
        return []
    serialized = []
    for tool_call in tool_calls:
        fn = getattr(tool_call, "function", None)
        name = getattr(fn, "name", None) if fn else None
        arguments = getattr(fn, "arguments", None) if fn else None
        safe_arguments = _safe_value(arguments, parse_json_strings=False)
        serialized.append({
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", None) or "function",
            "name": name,
            "arguments": safe_arguments,
            "function": {
                "name": name,
                "arguments": safe_arguments,
            },
        })
    return serialized


def _serialize_assistant_message(message: Any) -> dict[str, Any]:
    return {
        "content": _safe_value(getattr(message, "content", None)),
        "reasoning": _safe_value(getattr(message, "reasoning", None)),
        "tool_calls": _serialize_tool_calls(getattr(message, "tool_calls", None)),
    }


def _usage_and_cost(response: Any, *, provider: str, api_mode: str, model: str, base_url: str) -> tuple[dict[str, int], dict[str, float]]:
    usage_details: Dict[str, int] = {}
    cost_details: Dict[str, float] = {}
    raw_usage = getattr(response, "usage", None)
    if not raw_usage:
        return usage_details, cost_details

    try:
        from agent.usage_pricing import estimate_usage_cost, normalize_usage

        canonical = normalize_usage(raw_usage, provider=provider, api_mode=api_mode)
        # Langfuse usage_details keys follow a naming convention:
        #   - Dashboard sums all keys containing "input" as input total
        #   - Dashboard sums all keys containing "output" as output total
        #   - If no "total" key, Langfuse derives it from all usage types
        # Use Anthropic-style key names so cache tokens roll into the
        # dashboard input total automatically.
        # Ref: https://langfuse.com/docs/model-usage-and-cost
        usage_details = {
            "input": canonical.input_tokens,
            "output": canonical.output_tokens,
        }
        if canonical.cache_read_tokens:
            usage_details["cache_read_input_tokens"] = canonical.cache_read_tokens
        if canonical.cache_write_tokens:
            usage_details["cache_creation_input_tokens"] = canonical.cache_write_tokens
        if canonical.reasoning_tokens:
            usage_details["reasoning_tokens"] = canonical.reasoning_tokens
        cost = estimate_usage_cost(
            model,
            canonical,
            provider=provider,
            base_url=base_url,
            api_key="",
        )
        if cost.amount_usd is not None:
            # Langfuse cost_details keys must match usage_details keys.
            # Provide per-type breakdown so dashboard can show cost by type.
            try:
                from agent.usage_pricing import get_pricing_entry
                from decimal import Decimal
                _ONE_M = Decimal("1000000")
                entry = get_pricing_entry(model, provider=provider, base_url=base_url)
                if entry:
                    if entry.input_cost_per_million is not None and canonical.input_tokens:
                        cost_details["input"] = float(Decimal(canonical.input_tokens) * entry.input_cost_per_million / _ONE_M)
                    if entry.output_cost_per_million is not None and canonical.output_tokens:
                        cost_details["output"] = float(Decimal(canonical.output_tokens) * entry.output_cost_per_million / _ONE_M)
                    if entry.cache_read_cost_per_million is not None and canonical.cache_read_tokens:
                        cost_details["cache_read_input_tokens"] = float(Decimal(canonical.cache_read_tokens) * entry.cache_read_cost_per_million / _ONE_M)
                    if entry.cache_write_cost_per_million is not None and canonical.cache_write_tokens:
                        cost_details["cache_creation_input_tokens"] = float(Decimal(canonical.cache_write_tokens) * entry.cache_write_cost_per_million / _ONE_M)
                else:
                    cost_details["total"] = float(cost.amount_usd)
            except Exception:
                cost_details["total"] = float(cost.amount_usd)
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"usage normalization failed: {exc}")

    return usage_details, cost_details


def _start_root_trace(task_key: str, *, task_id: str, session_id: str, platform: str, provider: str, model: str,
                      api_mode: str, messages: Any, client: Langfuse,
                      turn_id: str = "", api_request_id: str = "",
                      model_selection: Optional[dict] = None) -> TraceState:
    trace_id = client.create_trace_id(seed=f"{session_id or 'sessionless'}::{task_id or task_key}")
    trace_input = _extract_last_user_message(messages)
    metadata = {
        "source": "hermes",
        "task_id": task_id,
        "turn_id": turn_id,
        "api_request_id": api_request_id,
        "platform": platform,
        "provider": provider,
        "model": model,
        "api_mode": api_mode,
    }
    if _full_content_enabled():
        metadata.update(full_content=True, captured_scope="hermes_dispatch_attempt", redacted=True)
        metadata = _metadata_value(metadata)

    if model_selection is not None:
        metadata["model_selection"] = _metadata_value(model_selection) if _full_content_enabled() else _safe_value(model_selection)

    # session_id must be passed in trace_context for Langfuse session grouping.
    trace_ctx: Dict[str, Any] = {"trace_id": trace_id}
    if session_id:
        trace_ctx["session_id"] = session_id

    if propagate_attributes is not None:
        try:
            with propagate_attributes(
                session_id=session_id or task_key,
                trace_name="Hermes turn",
                tags=["hermes", "langfuse"],
            ):
                root_ctx = client.start_as_current_observation(
                    trace_context=trace_ctx,
                    name="Hermes turn",
                    as_type="chain",
                    input=trace_input,
                    metadata=metadata,
                    end_on_exit=False,
                )
                root_span = root_ctx.__enter__()
        except Exception:
            root_ctx = client.start_as_current_observation(
                trace_context=trace_ctx,
                name="Hermes turn",
                as_type="chain",
                input=trace_input,
                metadata=metadata,
                end_on_exit=False,
            )
            root_span = root_ctx.__enter__()
    else:
        root_ctx = client.start_as_current_observation(
            trace_context=trace_ctx,
            name="Hermes turn",
            as_type="chain",
            input=trace_input,
            metadata=metadata,
            end_on_exit=False,
        )
        root_span = root_ctx.__enter__()

    try:
        root_span.set_trace_io(input=trace_input)
    except Exception:
        pass

    _debug(f"started trace {trace_id} for {task_key}")
    return TraceState(trace_id=trace_id, root_ctx=root_ctx, root_span=root_span)


def _start_child_observation(state: TraceState, *, client: Langfuse, name: str, as_type: str,
                             input_value: Any, metadata: Optional[dict] = None,
                             model: Optional[str] = None, model_parameters: Optional[dict] = None) -> Any:
    return state.root_span.start_observation(
        name=name,
        as_type=as_type,
        input=input_value,
        metadata=_metadata_value(metadata or {}),
        model=_content_value(model) if _full_content_enabled() else model,
        model_parameters=_metadata_value(model_parameters),
    )


def _end_observation(observation: Any, *, output: Any = None, metadata: Optional[dict] = None,
                     usage_details: Optional[dict] = None, cost_details: Optional[dict] = None) -> None:
    if observation is None:
        return
    try:
        update_kwargs: Dict[str, Any] = {}
        if output is not None:
            update_kwargs["output"] = output
        if metadata:
            update_kwargs["metadata"] = metadata
        if usage_details:
            update_kwargs["usage_details"] = usage_details
        if cost_details:
            update_kwargs["cost_details"] = cost_details
        if update_kwargs:
            observation.update(**update_kwargs)
        observation.end()
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"end observation failed: {exc}")


def _merge_trace_output(output: Any, state: TraceState) -> Any:
    if not state.turn_tool_calls:
        return output

    merged = dict(output) if isinstance(output, dict) else {"content": output}
    merged["tool_calls"] = list(state.turn_tool_calls)
    return merged


def _evict_stale_locked() -> None:
    """Drop least-recently-updated trace state to make room for a new entry.

    Caller MUST hold ``_STATE_LOCK`` and call this immediately before inserting
    one new entry. Bounds the leak from turns that never reach ``_finish_trace``
    (interrupted / tool-only final step / empty final content), whose unique
    per-turn key would otherwise linger forever. We evict down to
    ``_MAX_TRACE_STATE - 1`` so that the about-to-be-added entry leaves the dict
    at ``_MAX_TRACE_STATE`` — a true ceiling. The evicted entry's root span is
    ended so it is not left dangling on the Langfuse side.
    """
    over = len(_TRACE_STATE) - (_MAX_TRACE_STATE - 1)
    if over <= 0:
        return
    # Oldest-first by last_updated_at; evict just enough to make room.
    stale = sorted(_TRACE_STATE.items(), key=lambda kv: kv[1].last_updated_at)[:over]
    for key, state in stale:
        _TRACE_STATE.pop(key, None)
        try:
            state.root_span.end()
        except Exception as exc:  # pragma: no cover - fail-open
            _debug(f"evict stale trace failed: {exc}")


def _finish_trace(task_key: str, *, output: Any = None) -> None:
    client = _get_langfuse()
    if client is None:
        return

    with _STATE_LOCK:
        state = _TRACE_STATE.pop(task_key, None)
    if state is None:
        return

    try:
        for observation in state.generations.values():
            _end_observation(observation)
        for observation in state.tools.values():
            _end_observation(observation)
        for queue in state.pending_tools_by_name.values():
            for observation in queue:
                _end_observation(observation)
        final_output = _merge_trace_output(output, state)
        if final_output is not None:
            state.root_span.set_trace_io(output=final_output)
            state.root_span.update(output=final_output)
        state.root_span.end()
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"finish trace failed: {exc}")
    finally:
        try:
            client.flush()
        except Exception:
            pass


def _assistant_has_tool_calls(message: Any) -> bool:
    return bool(getattr(message, "tool_calls", None))


def _request_key(api_call_count: Any) -> str:
    return str(api_call_count or 0)


def on_pre_llm_call(*, task_id: str = "", session_id: str = "", platform: str = "", model: str = "",
                    provider: str = "", base_url: str = "", api_mode: str = "",
                    api_call_count: int = 0, messages: Any = None, turn_type: str = "user",
                    conversation_history: Any = None, user_message: Any = None,
                    turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    if _full_content_enabled():
        return
    # Older Hermes branches used pre_llm_call for request-scoped tracing and
    # passed the actual API messages. Current Hermes also has a turn-scoped
    # pre_llm_call used for context injection; tracing that hook creates an
    # extra orphan/root trace before the real request trace. Only trace the
    # legacy request-shaped call here.
    if not isinstance(messages, list):
        return

    client = _get_langfuse()
    if client is None:
        return

    # messages is a list only for legacy Hermes branches that fired
    # pre_llm_call with API messages directly. Current Hermes fires
    # pre_llm_call for context injection (conversation_history/user_message,
    # no messages list) — tracing that would create orphan traces.
    task_key = _trace_key(
        task_id,
        session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
    )

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = _start_root_trace(
                task_key,
                task_id=task_id,
                session_id=session_id,
                platform=platform,
                provider=provider,
                model=model,
                api_mode=api_mode,
                messages=messages,
                client=client,
                turn_id=turn_id,
                api_request_id=api_request_id,
            )
            _evict_stale_locked()
            _TRACE_STATE[task_key] = state
        state.last_updated_at = time.time()


def on_pre_llm_request(
    *,
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    request_messages: Any = None,
    messages: Any = None,
    turn_type: str = "user",
    message_count: int = 0,
    tool_count: int = 0,
    approx_input_tokens: int = 0,
    request_char_count: int = 0,
    max_tokens: Any = None,
    conversation_history: Any = None,
    user_message: Any = None,
    turn_id: str = "",
    api_request_id: str = "",
    model_selection: Optional[dict] = None,
    **_: Any,
) -> None:
    if _full_content_enabled():
        return
    client = _get_langfuse()
    if client is None:
        return

    input_messages = _coerce_request_messages(
        request_messages=request_messages,
        messages=messages,
        conversation_history=conversation_history,
        user_message=user_message,
    )

    task_key = _trace_key(
        task_id,
        session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
    )
    req_key = _request_key(api_call_count)

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = _start_root_trace(
                task_key,
                task_id=task_id,
                session_id=session_id,
                platform=platform,
                provider=provider,
                model=model,
                api_mode=api_mode,
                messages=input_messages,
                client=client,
                turn_id=turn_id,
                api_request_id=api_request_id,
                model_selection=model_selection,
            )
            _evict_stale_locked()
            _TRACE_STATE[task_key] = state
        state.last_updated_at = time.time()
        previous = state.generations.pop(req_key, None)
        if previous is not None:
            _end_observation(previous)
        state.generations[req_key] = _start_child_observation(
            state,
            client=client,
            name=f"LLM call {api_call_count}",
            as_type="generation",
            input_value=_serialize_messages(input_messages),
            metadata={
                "provider": provider,
                "platform": platform,
                "api_mode": api_mode,
                "base_url": base_url,
                **({"model_selection": _safe_value(model_selection)}
                   if model_selection is not None else {}),
            },
            model=model,
            model_parameters={"api_mode": api_mode, "provider": provider},
        )


def on_pre_api_attempt(*, request: Any = None, api_attempt_id: str = "",
                       task_id: str = "", session_id: str = "", turn_id: str = "",
                       api_request_id: str = "", api_call_count: int = 0,
                       platform: str = "", provider: str = "", model: str = "",
                       api_mode: str = "", base_url: str = "",
                       model_selection: Optional[dict] = None, **_: Any) -> None:
    """Capture the synchronous snapshot before this Hermes dispatch attempt."""
    if not _full_content_enabled():
        return
    client = _get_langfuse()
    if client is None:
        return
    # Snapshot before returning to the retry machinery; kwargs can be mutated.
    source = _full_value(request)
    messages = source.get("messages", source.get("input", [])) if isinstance(source, dict) else []
    task_key = _trace_key(task_id, session_id, turn_id=turn_id, api_request_id=api_request_id)
    metadata = _metadata_value({
        "full_content": True, "captured_scope": "hermes_dispatch_attempt", "redacted": True,
        "api_attempt_id": api_attempt_id, "api_request_id": api_request_id,
        "provider": provider, "platform": platform, "api_mode": api_mode, "base_url": base_url,
        "available_message_count": len(messages) if isinstance(messages, list) else None,
        **({"model_selection": model_selection} if model_selection is not None else {}),
    })
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = _start_root_trace(
                task_key, task_id=task_id, session_id=session_id, platform=platform,
                provider=provider, model=model, api_mode=api_mode, messages=messages,
                client=client, turn_id=turn_id, api_request_id=api_request_id,
                model_selection=model_selection,
            )
            _evict_stale_locked()
            _TRACE_STATE[task_key] = state
        state.last_updated_at = time.time()
        state.generations[api_attempt_id] = _start_child_observation(
            state, client=client, name=f"LLM attempt {api_attempt_id}", as_type="generation",
            input_value=source, metadata=metadata, model=model,
            model_parameters={"api_mode": api_mode, "provider": provider},
        )


def on_post_api_attempt(*, response: Any = None, error: Any = None, partial: Any = None,
                        api_attempt_id: str = "", task_id: str = "", session_id: str = "",
                        turn_id: str = "", api_request_id: str = "", provider: str = "",
                        model: str = "", api_mode: str = "", base_url: str = "",
                        duration: float = 0.0, **_: Any) -> None:
    if not _full_content_enabled():
        return
    task_key = _trace_key(task_id, session_id, turn_id=turn_id, api_request_id=api_request_id)
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        generation = state.generations.pop(api_attempt_id, None) if state else None
    if generation is None:
        return
    output = {"error": _full_value(error), "partial": _full_value(partial)} if error is not None else _full_value(response)
    usage_details, cost_details = _usage_and_cost(
        response, provider=provider, api_mode=api_mode, model=model, base_url=base_url,
    )
    _end_observation(
        generation, output=output, usage_details=usage_details, cost_details=cost_details,
        metadata=_metadata_value({"api_attempt_id": api_attempt_id, "status": "error" if error else "success",
                                  "duration_s": duration, "full_content": True,
                                  "captured_scope": "hermes_dispatch_attempt", "redacted": True}),
    )


def on_post_llm_call(*, task_id: str = "", session_id: str = "", provider: str = "", base_url: str = "",
                     api_mode: str = "", model: str = "", api_call_count: int = 0,
                     assistant_message: Any = None, response: Any = None,
                     api_duration: float = 0.0, finish_reason: str = "",
                     usage: Any = None, assistant_content_chars: int = 0,
                     assistant_tool_call_count: int = 0, assistant_response: Any = None,
                     turn_id: str = "", api_request_id: str = "",
                     **_: Any) -> None:
    if _full_content_enabled():
        # post_api_request carries api_request_id; only the later turn hook
        # closes the chain. It must not consume an in-flight attempt span.
        if not api_request_id:
            output = assistant_message if assistant_message is not None else assistant_response
            _finish_trace(_trace_key(task_id, session_id, turn_id=turn_id), output=_full_value(output))
        return
    client = _get_langfuse()
    if client is None:
        return

    task_key = _trace_key(
        task_id,
        session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
    )
    req_key = _request_key(api_call_count)

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        generation = state.generations.pop(req_key, None) if state else None
    if state is None or generation is None:
        return

    # Handle both call patterns:
    # 1. post_api_request: passes usage (dict), assistant_content_chars, assistant_tool_call_count
    # 2. post_llm_call: passes assistant_message (object), response (object), assistant_response (str)
    if assistant_message is not None:
        output = _serialize_assistant_message(assistant_message)
    elif assistant_response is not None:
        # post_llm_call passes assistant_response as a plain string
        output = {"content": _safe_value(assistant_response), "reasoning": None, "tool_calls": []}
    else:
        # post_api_request path — reconstruct from summary kwargs
        output = {
            "content": f"[{assistant_content_chars} chars]" if assistant_content_chars else None,
            "reasoning": None,
            "tool_calls": [{"id": f"tc_{i}"} for i in range(assistant_tool_call_count)] if assistant_tool_call_count else [],
        }

    if output.get("tool_calls"):
        state.turn_tool_calls.extend(output["tool_calls"])

    # Extract usage: prefer a real response object that carries usage, else
    # fall back to the usage summary dict from post_api_request.
    #
    # post_api_request passes `response` as a SANITIZED dict (no ``.usage``
    # attribute) alongside a separate `usage` summary dict. Gating on
    # ``response is not None`` here took the response-object path on that dict,
    # where ``getattr(response, "usage", None)`` is always None — so usage and
    # cost were silently dropped for every gateway turn. Gate on a real
    # ``.usage`` attribute instead so the usage-dict fallback below is reached.
    if getattr(response, "usage", None) is not None:
        usage_details, cost_details = _usage_and_cost(
            response,
            provider=provider,
            api_mode=api_mode,
            model=model,
            base_url=base_url,
        )
    elif isinstance(usage, dict) and usage:
        # post_api_request passes a pre-built CanonicalUsage summary dict.
        # Use Langfuse-convention key names: "input", "output", and
        # "cache_read_input_tokens" / "cache_creation_input_tokens" so the
        # dashboard sums cache tokens into the input total automatically.
        _input = usage.get("input_tokens", 0)
        _output = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        _cache_read = usage.get("cache_read_tokens", 0)
        _cache_write = usage.get("cache_write_tokens", 0)
        _reasoning = usage.get("reasoning_tokens", 0)
        usage_details = {
            "input": _input,
            "output": _output,
        }
        if _cache_read:
            usage_details["cache_read_input_tokens"] = _cache_read
        if _cache_write:
            usage_details["cache_creation_input_tokens"] = _cache_write
        if _reasoning:
            usage_details["reasoning_tokens"] = _reasoning
        cost_details = {}
        # Estimate per-type cost from the summary if possible
        try:
            from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry
            from decimal import Decimal
            _ONE_M = Decimal("1000000")
            _cu = CanonicalUsage(
                input_tokens=_input,
                output_tokens=_output,
                cache_read_tokens=_cache_read,
                cache_write_tokens=_cache_write,
                reasoning_tokens=_reasoning,
            )
            entry = get_pricing_entry(model, provider=provider, base_url=base_url)
            if entry:
                if entry.input_cost_per_million is not None and _input:
                    cost_details["input"] = float(Decimal(_input) * entry.input_cost_per_million / _ONE_M)
                if entry.output_cost_per_million is not None and _output:
                    cost_details["output"] = float(Decimal(_output) * entry.output_cost_per_million / _ONE_M)
                if entry.cache_read_cost_per_million is not None and _cache_read:
                    cost_details["cache_read_input_tokens"] = float(Decimal(_cache_read) * entry.cache_read_cost_per_million / _ONE_M)
                if entry.cache_write_cost_per_million is not None and _cache_write:
                    cost_details["cache_creation_input_tokens"] = float(Decimal(_cache_write) * entry.cache_write_cost_per_million / _ONE_M)
            else:
                _cost = estimate_usage_cost(model, _cu, provider=provider, base_url=base_url, api_key="")
                if _cost.amount_usd is not None:
                    cost_details["total"] = float(_cost.amount_usd)
        except Exception:
            pass
    else:
        usage_details, cost_details = {}, {}

    tool_count = len(output.get("tool_calls", [])) or assistant_tool_call_count
    gen_metadata: Dict[str, Any] = {"tool_call_count": tool_count}
    if api_duration and api_duration > 0:
        gen_metadata["api_duration_s"] = round(api_duration, 3)
    if finish_reason:
        gen_metadata["finish_reason"] = finish_reason
    _end_observation(
        generation,
        output=output,
        usage_details=usage_details,
        cost_details=cost_details,
        metadata=gen_metadata,
    )

    has_tools = _assistant_has_tool_calls(assistant_message) if assistant_message else (assistant_tool_call_count > 0)
    has_content = bool(output.get("content"))
    if not has_tools and has_content:
        _finish_trace(task_key, output=output)


def on_pre_tool_call(*, tool_name: str = "", args: Any = None, task_id: str = "",
                     session_id: str = "", tool_call_id: str = "",
                     turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    client = _get_langfuse()
    if client is None:
        return

    task_key = _trace_key(
        task_id,
        session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
    )

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        observation = _start_child_observation(
            state,
            client=client,
            name=f"Tool: {tool_name}",
            as_type="tool",
            input_value=_content_value(args),
            metadata={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )
        if tool_call_id:
            state.tools[tool_call_id] = observation
        else:
            state.pending_tools_by_name.setdefault(tool_name, []).append(observation)


def on_post_tool_call(*, tool_name: str = "", args: Any = None, result: Any = None,
                      task_id: str = "", session_id: str = "", tool_call_id: str = "",
                      turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    task_key = _trace_key(
        task_id,
        session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
    )
    observation = None

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        if tool_call_id:
            observation = state.tools.pop(tool_call_id, None)
        if observation is None:
            queue = state.pending_tools_by_name.get(tool_name)
            if queue:
                observation = queue.pop(0)
                if not queue:
                    state.pending_tools_by_name.pop(tool_name, None)

    if observation is None:
        return

    if _full_content_enabled():
        safe_result_value = _full_value(result)
    elif isinstance(result, str):
        result_value = _maybe_parse_json_string(result)
    else:
        result_value = result
    if not _full_content_enabled():
        result_value = _normalize_payload(result_value, tool_name=tool_name, args=args)
        safe_result_value = _safe_value(result_value, parse_json_strings=True)

    # Backfill so the generation's tool_call record carries the result alongside arguments.
    if tool_call_id:
        with _STATE_LOCK:
            state = _TRACE_STATE.get(task_key)
            if state is not None:
                for tool_call in reversed(state.turn_tool_calls):
                    if tool_call.get("id") == tool_call_id:
                        tool_call["output"] = safe_result_value
                        function_payload = tool_call.get("function")
                        if isinstance(function_payload, dict):
                            function_payload["output"] = safe_result_value
                        break

    _end_observation(
        observation,
        output=safe_result_value,
        metadata=_metadata_value({"tool_name": tool_name, "args": _content_value(args, parse_json_strings=True)}),
    )


def register(ctx) -> None:
    # Register for both hook name variants so the plugin works across
    # Hermes versions.  pre_api_request / post_api_request fire per API
    # call (preferred); pre_llm_call / post_llm_call fire once per turn.
    ctx.register_hook("pre_api_request", on_pre_llm_request)
    ctx.register_hook("post_api_request", on_post_llm_call)
    ctx.register_hook("pre_api_attempt", on_pre_api_attempt)
    ctx.register_hook("post_api_attempt", on_post_api_attempt)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
