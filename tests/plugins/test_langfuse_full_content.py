"""Opt-in complete model content at the Langfuse SDK boundary (no network)."""
from __future__ import annotations

import base64
import importlib
import json
import sys
from types import SimpleNamespace

import pytest
import yaml


class RecordingSpan:
    def __init__(self, **kwargs):
        self.started = kwargs
        self.updates = []
        self.children = []
        self.ended = False

    def start_observation(self, **kwargs):
        span = RecordingSpan(**kwargs)
        self.children.append(span)
        return span

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def set_trace_io(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True

    def __enter__(self):
        return self


class RecordingClient:
    def __init__(self):
        self.roots = []

    def create_trace_id(self, seed):
        return "0" * 32

    def start_as_current_observation(self, **kwargs):
        span = RecordingSpan(**kwargs)
        self.roots.append(span)
        return span

    def flush(self):
        pass


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    sys.modules.pop("plugins.observability.langfuse", None)
    mod = importlib.import_module("plugins.observability.langfuse")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {"observability/langfuse": {"full_content": True}}},
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hooks = {}
    mod.register(SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, fn)))
    client = RecordingClient()
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    monkeypatch.setattr(mod, "propagate_attributes", None)
    return mod, client, hooks


def attempt_kwargs(number=1):
    return dict(task_id="task", session_id="session", turn_id="turn",
                api_request_id="request", api_attempt_id=f"request:attempt:{number}",
                api_call_count=1, model="synthetic-model", provider="synthetic")


def test_full_request_reaches_sdk_without_preview_limits(plugin):
    _, client, hooks = plugin
    nested = {"level": {"level": {"level": {"level": {"complete": "end"}}}}}
    messages = [{"role": "system", "content": "policy " + "s" * 15000}]
    messages += [{"role": "user", "content": f"message {i}"} for i in range(15)]
    messages += [{"role": "tool", "content": json.dumps({"items": list(range(75)), **nested})}]
    request = {"messages": messages, "tools": [{"name": f"tool_{i}"} for i in range(75)]}
    request["tools"][0]["parameters"] = {"type": "object", "properties": {
        "api_key": {"type": "string", "description": "credential input"}, "run_token": {},
    }}
    request["messages"].append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "YQ==" * 5000}},
    ]})
    hooks["pre_api_attempt"](request=request, **attempt_kwargs())
    generation = client.roots[0].children[0]
    assert generation.started["input"] == request
    assert generation.started["metadata"]["captured_scope"] == "hermes_dispatch_attempt"
    assert generation.started["metadata"]["full_content"] is True
    assert generation.started["metadata"]["redacted"] is True
    # The SDK retains a snapshot, independent of provider retry mutations.
    request["messages"][0]["content"] = "mutated"
    assert generation.started["input"]["messages"][0]["content"].endswith("s" * 15000)


def test_complete_answer_and_exposed_reasoning_reach_sdk(plugin):
    _, client, hooks = plugin
    hooks["pre_api_attempt"](request={"input": "question"}, **attempt_kwargs())
    output = {"choices": [{"message": {"content": "a" * 16000,
              "reasoning_content": "r" * 17000, "tool_calls": [{"id": str(i)} for i in range(75)]}}]}
    response = SimpleNamespace(**output)
    hooks["post_api_attempt"](response=response, error=None, api_duration=0.5, **attempt_kwargs())
    generation = client.roots[0].children[0]
    assert generation.updates[-1]["output"] == output
    assert generation.ended


def test_full_capture_keeps_model_selection_receipt_at_root_and_generation(plugin):
    _, client, hooks = plugin
    receipt = {"selected_model": "selected-target", "effective_model": "fallback-actual",
               "effective_provider": "synthetic", "effective_reasoning": {"reasoning_effort": "high"},
               "effective_service_tier": "priority", "api_key": "synthetic-receipt-secret"}
    kwargs = {**attempt_kwargs(), "model": "fallback-actual", "model_selection": receipt}
    hooks["pre_api_attempt"](request={"model": "fallback-actual"}, **kwargs)
    root = client.roots[0]
    generation = root.children[0]
    for metadata in (root.started["metadata"], generation.started["metadata"]):
        selected = metadata["model_selection"]
        assert selected["selected_model"] == "selected-target"
        assert selected["effective_model"] == "fallback-actual"
        assert selected["effective_reasoning"] == {"reasoning_effort": "high"}
        assert selected["effective_service_tier"] == "priority"
        assert selected["api_key"] == "[REDACTED]"
    assert generation.started["model"] == "fallback-actual"


def test_failed_and_successful_attempts_are_distinct(plugin):
    _, client, hooks = plugin
    hooks["pre_api_attempt"](request={"messages": [{"content": "unicode ☃"}]}, **attempt_kwargs())
    hooks["post_api_attempt"](response=None, error=ValueError("invalid unicode"), **attempt_kwargs())
    hooks["pre_api_attempt"](request={"messages": [{"content": "ascii"}]}, **attempt_kwargs(2))
    hooks["post_api_attempt"](response={"content": "answer"}, error=None, **attempt_kwargs(2))
    children = client.roots[0].children
    assert len(children) == 2
    assert children[0].started["input"]["messages"][0]["content"] == "unicode ☃"
    assert children[0].updates[-1]["output"]["error"]["message"] == "invalid unicode"
    assert children[0].ended and children[1].ended


def test_credentials_redacted_in_prose_json_and_metadata(plugin, monkeypatch):
    _, client, hooks = plugin
    monkeypatch.setenv("SYNTHETIC_API_KEY", "opaque-credential-value")
    request = {"messages": [{"role": "system", "content":
        'Credential opaque-credential-value; run_token=unconfigured-run-credential; '
        '{"run_token": "json-run-credential", "data": "kept"}'}],
        "headers": {"Authorization": "Bearer header-credential"},
        "extra": {"run_token": "structured-run-credential"}}
    hooks["pre_api_attempt"](request=request, base_url="https://example.test/?token=url-credential",
                              **attempt_kwargs())
    serialized = json.dumps(client.roots[0].started) + json.dumps(client.roots[0].children[0].started)
    for secret in ("opaque-credential-value", "unconfigured-run-credential", "json-run-credential",
                   "header-credential", "structured-run-credential", "url-credential"):
        assert secret not in serialized
    assert "kept" in serialized


def test_full_tool_result_preserves_read_file_and_all_items(plugin):
    _, client, hooks = plugin
    hooks["pre_api_attempt"](request={"messages": []}, **attempt_kwargs())
    arguments = {"items": list(range(75))}
    hooks["pre_tool_call"](tool_name="read_file", args=arguments, tool_call_id="tool", **attempt_kwargs())
    result = {"content": "\n".join(f"{i}|line {i}" for i in range(1, 90)),
              "total_lines": 89, "file_size": 15000, "is_binary": False, "is_image": False}
    hooks["post_tool_call"](tool_name="read_file", args=arguments, result=json.dumps(result),
                            tool_call_id="tool", **attempt_kwargs())
    tool = client.roots[0].children[1]
    assert tool.started["input"] == arguments
    assert tool.updates[-1]["output"] == json.dumps(result)


def test_full_mode_avoids_duplicate_preview_generation(plugin):
    _, client, hooks = plugin
    hooks["pre_api_request"](request_messages=[{"role": "user", "content": "preview"}], **attempt_kwargs())
    assert not client.roots
    hooks["pre_api_attempt"](request={"messages": []}, **attempt_kwargs())
    hooks["post_api_request"](assistant_content_chars=10, **attempt_kwargs())
    assert not client.roots[0].children[0].ended


def test_default_config_keeps_preview_mode(tmp_path, monkeypatch):
    sys.modules.pop("plugins.observability.langfuse", None)
    mod = importlib.import_module("plugins.observability.langfuse")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hooks = {}
    mod.register(SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, fn)))
    client = RecordingClient()
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    monkeypatch.setattr(mod, "propagate_attributes", None)
    hooks["pre_api_attempt"](request={"messages": []}, **attempt_kwargs())
    assert not client.roots
    hooks["pre_api_request"](request_messages=[{"role": "user", "content": str(i)} for i in range(15)],
                             **attempt_kwargs())
    assert len(client.roots[0].children[0].started["input"]) == 12


def test_nested_json_credentials_and_public_key_data(plugin):
    mod, _, _ = plugin
    value = json.dumps({"arguments": json.dumps({"run_token": "nested-opaque-value", "key": "public-data"})})
    safe = mod._full_value(value)
    assert "nested-opaque-value" not in safe
    assert json.loads(json.loads(safe)["arguments"])["key"] == "public-data"


def test_attempt_usage_cost_and_turn_output_are_forwarded(plugin, monkeypatch):
    mod, client, hooks = plugin
    monkeypatch.setattr(mod, "_usage_and_cost", lambda *a, **k: ({"input": 123, "output": 45}, {"total": 0.01}))
    hooks["pre_api_attempt"](request={"messages": []}, **attempt_kwargs())
    hooks["post_api_attempt"](response={"content": "answer"}, duration=0.25, **attempt_kwargs())
    update = client.roots[0].children[0].updates[-1]
    assert update["usage_details"] == {"input": 123, "output": 45}
    assert update["cost_details"] == {"total": 0.01}
    assert update["metadata"]["duration_s"] == 0.25
    hooks["post_llm_call"](task_id="task", session_id="session", turn_id="turn", assistant_response="a" * 16000)
    assert client.roots[0].updates[-1]["output"] == "a" * 16000
    assert client.roots[0].ended
    assert not mod._TRACE_STATE


def test_non_json_types_are_explicit_and_json_safe(plugin):
    mod, _, _ = plugin
    cycle = []
    cycle.append(cycle)
    safe = mod._full_value({"bytes": b"abc", "not_finite": float("nan"), "cycle": cycle})
    json.dumps(safe, allow_nan=False)
    assert safe["bytes"] == {"type": "bytes", "omitted": True,
                             "reason": "opaque binary may contain credentials", "length": 3}
    assert safe["cycle"][0]["reason"] == "cyclic non-JSON value"


def test_byte_credentials_are_not_exported_in_nested_requests_or_tool_results(plugin):
    _, client, hooks = plugin
    secret = b"synthetic-opaque-credential"
    hooks["pre_api_attempt"](request={"ordinary": [bytearray(secret), {"payload": b"\xff" + secret}]},
                              **attempt_kwargs())
    hooks["pre_tool_call"](tool_name="example", args={}, tool_call_id="tool", **attempt_kwargs())
    hooks["post_tool_call"](tool_name="example", args={}, result={"payload": secret},
                            tool_call_id="tool", **attempt_kwargs())
    generation, tool = client.roots[0].children
    observed = json.dumps([generation.started, tool.updates])
    assert secret.decode() not in observed
    for raw in (secret, b"\xff" + secret):
        assert base64.b64encode(raw).decode() not in observed
    assert '"omitted": true' in observed


def test_nonstring_mapping_keys_cannot_leak_or_overwrite_other_keys(plugin):
    mod, _, _ = plugin
    secret = b"synthetic-secret-in-key"
    value = {"[omitted non-string key]": "existing", secret: "hidden value",
             ("nested", secret): "another hidden value", "other": "preserved"}
    captured = mod._full_value(value)
    serialized = json.dumps(captured)
    assert secret.decode() not in serialized
    assert "hidden value" not in serialized
    assert captured["[omitted non-string key]"] == "existing"
    assert captured["other"] == "preserved"
    assert len(captured) == len(value)
    assert sum(v == "[OMITTED: non-string mapping key]" for v in captured.values()) == 2


def test_exception_byte_arguments_are_never_stringified_for_export(plugin):
    mod, _, _ = plugin
    secret = b"synthetic-secret-in-exception"
    captured = mod._full_value(ValueError(secret, {"nested": [secret]}))
    serialized = json.dumps(captured)
    assert secret.decode() not in serialized
    assert base64.b64encode(secret).decode() not in serialized
    assert "opaque binary may contain credentials" in serialized


def test_real_sdk_excludes_binary_keys_from_request_and_tool_result(plugin, monkeypatch):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mod, _, hooks = plugin
    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(public_key="pk-lf-test-binary-key", secret_key="sk-lf-test",
                              span_exporter=exporter, sample_rate=1.0)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    secret = b"synthetic-secret-in-binary-key"
    try:
        hooks["pre_api_attempt"](request={"nested": {secret: "private-key-value"}}, **attempt_kwargs())
        hooks["pre_tool_call"](tool_name="example", args={}, tool_call_id="tool", **attempt_kwargs())
        hooks["post_tool_call"](tool_name="example", args={}, result={"nested": {("a", secret): "private-value"}},
                                tool_call_id="tool", **attempt_kwargs())
        hooks["post_api_attempt"](response={"content": "safe"}, **attempt_kwargs())
        hooks["post_llm_call"](task_id="task", session_id="session", turn_id="turn", assistant_response="safe")
        client.flush()
        attributes = json.dumps([dict(span.attributes) for span in exporter.get_finished_spans()])
        assert secret.decode() not in attributes
        assert "private-key-value" not in attributes
        assert "private-value" not in attributes
        assert "[OMITTED: non-string mapping key]" in attributes
    finally:
        client.shutdown()


def test_credential_key_variants_preserve_usage_and_schema_definitions(plugin):
    mod, _, _ = plugin
    controls = {"input_tokens": 12, "output_tokens": 5, "reasoning_tokens": 3,
                "max_tokens": 200, "token_count": 17, "key": "public-data"}
    value = {**controls, "secretKey": "opaque-value", "AWS_SECRET_ACCESS_KEY": "other-value",
             "schema": {"$defs": {"api_key": {"type": "string", "description": "Input definition"}}}}
    safe = mod._full_value(value)
    assert all(safe[key] == val for key, val in controls.items())
    assert safe["secretKey"] == safe["AWS_SECRET_ACCESS_KEY"] == "[REDACTED]"
    assert safe["schema"] == value["schema"]


def test_real_sdk_exports_complete_model_content(plugin, monkeypatch):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mod, _, hooks = plugin
    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(public_key="pk-lf-test-full-content-sdk", secret_key="sk-lf-test",
                              span_exporter=exporter, sample_rate=1.0)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    monkeypatch.setenv("SYNTHETIC_API_KEY", "opaque-sdk-credential")
    request = {"messages": [{"role": "system", "content": "s" * 15000}]
               + [{"role": "tool", "content": json.dumps({"items": list(range(75)),
                   "nested": {"a": {"b": {"c": {"d": {"e": "complete"}}}}}})} for _ in range(15)],
               "tools": [{"name": f"tool_{i}"} for i in range(75)]}
    formatted_json = json.dumps({"caption": "café", "token_count": 42, "values": [1, 2]},
                                indent=2, ensure_ascii=True)
    request["messages"].extend([
        {"role": "tool", "content": formatted_json},
        {"role": "tool", "content": json.dumps(formatted_json)},
    ])
    request["headers"] = {"Authorization": "Bearer opaque-sdk-credential"}
    request["attachment"] = b"synthetic-sdk-byte-secret"
    expected_request = {**request, "headers": {"Authorization": "[REDACTED]"},
                        "attachment": {"type": "bytes", "omitted": True,
                                       "reason": "opaque binary may contain credentials",
                                       "length": len(request["attachment"])}}
    response = {"choices": [{"message": {"content": "a" * 16000,
                 "reasoning": "r" * 17000, "tool_calls": [{"id": str(i)} for i in range(75)]}}]}
    try:
        hooks["pre_api_attempt"](request=request, base_url="https://example.test/?token=opaque-url-credential",
                                  **attempt_kwargs())
        hooks["post_api_attempt"](response=SimpleNamespace(**response), error=None, **attempt_kwargs())
        hooks["post_llm_call"](task_id="task", session_id="session", turn_id="turn",
                               assistant_response="a" * 16000)
        client.flush()
        spans = exporter.get_finished_spans()
        generation = next(span for span in spans if span.name.startswith("LLM attempt "))
        assert json.loads(generation.attributes["langfuse.observation.input"]) == expected_request
        assert json.loads(generation.attributes["langfuse.observation.output"]) == response
        assert generation.attributes["langfuse.observation.metadata.captured_scope"] == "hermes_dispatch_attempt"
        assert generation.attributes["langfuse.observation.metadata.redacted"] is True
        assert "opaque-sdk-credential" not in json.dumps(dict(generation.attributes))
        assert "opaque-url-credential" not in json.dumps(dict(generation.attributes))
        assert base64.b64encode(request["attachment"]).decode() not in json.dumps(dict(generation.attributes))
    finally:
        client.shutdown()


def test_real_plugin_manager_propagates_opt_in_and_attempt_hooks(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["observability/langfuse"],
        "entries": {"observability/langfuse": {"full_content": True}},
    }}))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["observability/langfuse"]
    assert loaded.enabled, loaded.error
    client = RecordingClient()
    monkeypatch.setattr(loaded.module, "_get_langfuse", lambda: client)
    monkeypatch.setattr(loaded.module, "propagate_attributes", None)
    assert manager.has_hook("pre_api_attempt") and manager.has_hook("post_api_attempt")
    request = {"messages": [{"role": "system", "content": "s" * 15000}]}
    manager.invoke_hook("pre_api_attempt", request=request, **attempt_kwargs())
    manager.invoke_hook("post_api_attempt", response={"content": "answer"}, **attempt_kwargs())
    manager.invoke_hook("post_llm_call", task_id="task", session_id="session", turn_id="turn",
                        assistant_response="answer")
    assert client.roots[0].children[0].started["input"] == request
    assert client.roots[0].ended


@pytest.mark.parametrize("case", ["secret_key", "environment_access_key", "encoded_json_string",
                                  "credential_property_default", "schema_object_default"])
def test_real_sdk_excludes_credentials_in_each_source_shape(plugin, monkeypatch, case):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mod, _, hooks = plugin
    secret = f"synthetic-opaque-{case}-credential"
    if case == "secret_key":
        request = {"secret_key": secret}
    elif case == "environment_access_key":
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
        request = {"messages": [{"role": "system", "content": f"Use {secret} for this call."}]}
    elif case == "encoded_json_string":
        request = {"messages": [{"role": "tool", "content": json.dumps(json.dumps({"run_token": secret}))}]}
    elif case == "credential_property_default":
        request = {"tools": [{"function": {"parameters": {"type": "object", "properties": {
            "api_key": {"type": "string", "description": "Credential input", "default": secret},
        }}}}]}
    else:
        request = {"tools": [{"function": {"parameters": {"type": "object", "default": {"token": secret}}}}]}
    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(public_key=f"pk-lf-test-credential-{case}", secret_key="sk-lf-test",
                              span_exporter=exporter, sample_rate=1.0)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    try:
        hooks["pre_api_attempt"](request=request, **attempt_kwargs())
        hooks["post_api_attempt"](response={"content": "safe answer"}, **attempt_kwargs())
        hooks["post_llm_call"](task_id="task", session_id="session", turn_id="turn",
                               assistant_response="safe answer")
        client.flush()
        spans = exporter.get_finished_spans()
        generation = next(span for span in spans if span.name.startswith("LLM attempt "))
        assert secret not in json.dumps(dict(generation.attributes))
        assert "[REDACTED]" in generation.attributes["langfuse.observation.input"]
        if case == "credential_property_default":
            captured = json.loads(generation.attributes["langfuse.observation.input"])
            prop = captured["tools"][0]["function"]["parameters"]["properties"]["api_key"]
            assert prop["type"] == "string" and prop["description"] == "Credential input"
    finally:
        client.shutdown()


def test_profile_policy_and_scoped_secrets_are_isolated_across_async_tasks(plugin, tmp_path):
    import asyncio
    from contextvars import copy_context
    from agent.secret_scope import set_multiplex_active, set_secret_scope, reset_secret_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    mod, client, hooks = plugin
    homes = []
    for name, enabled in (("enabled", True), ("disabled", False)):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {
            "observability/langfuse": {"full_content": enabled}}}}))
        homes.append(home)

    async def exercise():
        arrived = asyncio.Event()
        count = 0
        async def run(home, enabled):
            nonlocal count
            home_token = set_hermes_home_override(home)
            secret = "synthetic-profile-secret-" + home.name
            scope_token = set_secret_scope({"CUSTOM_CREDENTIAL": secret})
            try:
                count += 1
                if count == 2:
                    arrived.set()
                await arrived.wait()
                await asyncio.sleep(0)
                assert mod._full_content_enabled() is enabled
                # Exercise the same context copying used for gateway worker threads.
                thread_policy = await asyncio.to_thread(copy_context().run, mod._full_content_enabled)
                assert thread_policy is enabled
                hooks["pre_api_attempt"](
                    request={"message": "opaque value " + secret},
                    **{**attempt_kwargs(), "turn_id": home.name},
                )
                if enabled:
                    result = mod._full_value({"message": "opaque value " + secret})
                    assert result == {"message": "opaque value [REDACTED]"}
                return mod._content_value("x" * 13000)
            finally:
                reset_secret_scope(scope_token)
                reset_hermes_home_override(home_token)
        return await asyncio.gather(run(homes[0], True), run(homes[1], False))

    set_multiplex_active(True)
    try:
        full, preview = asyncio.run(exercise())
        assert len(full) == 13000
        assert len(preview) < 13000
        assert len(client.roots) == 1
        assert client.roots[0].children[0].started["input"] == {
            "message": "opaque value [REDACTED]"}
        assert not mod._full_content_enabled()
        assert mod._full_value("anything") == {"omitted": True, "reason": "profile secrets unavailable"}
    finally:
        set_multiplex_active(False)


def test_full_capture_omits_content_if_secret_scope_cannot_be_enumerated(plugin):
    from agent.secret_scope import set_secret_scope, reset_secret_scope

    mod, _, _ = plugin
    class BadScope(dict):
        def values(self):
            raise RuntimeError("unavailable")
    token = set_secret_scope(BadScope())
    try:
        assert mod._full_value("sensitive content") == {
            "omitted": True, "reason": "profile secrets unavailable"}
    finally:
        reset_secret_scope(token)


def test_multiplex_without_context_emits_no_request_tool_or_completion(plugin):
    from agent.secret_scope import set_multiplex_active
    _, client, hooks = plugin
    secret = "opaque-synthetic-profile-credential"
    set_multiplex_active(True)
    try:
        hooks["pre_api_request"](request_messages=[{"role": "user", "content": secret}],
                                 **attempt_kwargs())
        hooks["pre_tool_call"](tool_name="demo", args={"text": secret}, **attempt_kwargs())
        hooks["post_tool_call"](tool_name="demo", result=secret, **attempt_kwargs())
        hooks["post_llm_call"](assistant_response=secret, **attempt_kwargs())
        assert client.roots == []
    finally:
        set_multiplex_active(False)


def test_preview_hooks_redact_scoped_opaque_credentials(plugin, tmp_path):
    from agent.secret_scope import set_multiplex_active, set_secret_scope, reset_secret_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    _, client, hooks = plugin
    home = tmp_path / "preview"
    home.mkdir()
    (home / "config.yaml").write_text("plugins: {}\n")
    secret = "opaque-synthetic-profile-credential"
    set_multiplex_active(True)
    home_token = set_hermes_home_override(home)
    scope_token = set_secret_scope({"CUSTOM_CREDENTIAL": secret})
    try:
        kwargs = attempt_kwargs()
        hooks["pre_api_request"](request_messages=[{"role": "user", "content": secret}], **kwargs)
        hooks["pre_tool_call"](tool_name="demo", args={"text": secret}, tool_call_id="tool", **kwargs)
        hooks["post_tool_call"](tool_name="demo", result=secret, tool_call_id="tool", **kwargs)
        hooks["post_llm_call"](assistant_response=secret, **kwargs)
        root = client.roots[0]
        exported = repr((root.started, root.updates, [(c.started, c.updates) for c in root.children]))
        assert secret not in exported
        assert "[REDACTED]" in exported
    finally:
        reset_secret_scope(scope_token)
        reset_hermes_home_override(home_token)
        set_multiplex_active(False)


def test_malformed_profile_config_disables_former_opt_in(plugin, tmp_path):
    mod, _, _ = plugin
    assert mod._full_content_enabled()
    (tmp_path / "config.yaml").write_text("plugins: [invalid\n")
    assert not mod._full_content_enabled()


def _write_process_policy(root, *homes):
    entries = []
    for home in homes:
        info = home.stat()
        entries.append({"home": str(home.resolve()), "device": info.st_dev, "inode": info.st_ino})
    path = root / "langfuse-full-content-policy.json"
    path.write_text(json.dumps({"version": 1, "profiles": entries}))
    path.chmod(0o600)
    return path


def test_process_policy_is_scoped_across_concurrent_profiles(plugin, tmp_path):
    import asyncio
    from contextvars import copy_context
    from agent.secret_scope import set_multiplex_active, set_secret_scope, reset_secret_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    mod, _, _ = plugin
    homes = []
    for name in ("selected", "other"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("plugins: {}\n")
        homes.append(home)
    _write_process_policy(tmp_path, homes[0])

    async def run(home):
        profile_token = set_hermes_home_override(home)
        scope_token = set_secret_scope({"CUSTOM_CREDENTIAL": "profile-specific-secret"})
        try:
            await asyncio.sleep(0)
            return (mod._full_content_enabled(),
                    await asyncio.to_thread(copy_context().run, mod._full_content_enabled),
                    mod._content_value("x" * 13000))
        finally:
            reset_secret_scope(scope_token)
            reset_hermes_home_override(profile_token)

    async def both():
        return await asyncio.gather(*(run(home) for home in homes))

    set_multiplex_active(True)
    try:
        selected, other = asyncio.run(both())
        assert selected[:2] == (True, True)
        assert other[:2] == (False, False)
        assert len(selected[2]) == 13000
        assert len(other[2]) < 13000
    finally:
        set_multiplex_active(False)


def test_process_policy_fails_closed_and_respects_profile_config(plugin, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    mod, _, _ = plugin
    home = tmp_path / "selected"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("plugins: {}\n")
    token = set_hermes_home_override(home)
    try:
        assert not mod._full_content_enabled()
        policy = _write_process_policy(tmp_path, home)
        assert mod._full_content_enabled()
        alias = tmp_path / "profile-alias"
        alias.symlink_to(home, target_is_directory=True)
        alias_token = set_hermes_home_override(alias)
        try:
            assert not mod._full_content_enabled()
        finally:
            reset_hermes_home_override(alias_token)
        config.write_text("plugins:\n  entries:\n    observability/langfuse:\n      full_content: false\n")
        assert not mod._full_content_enabled()
        config.write_text("plugins: {}\n")
        policy.chmod(0o644)
        assert not mod._full_content_enabled()
        policy.chmod(0o600)
        policy.write_text('{"version": true, "profiles": []}')
        assert not mod._full_content_enabled()
        _write_process_policy(tmp_path, home)
        data = json.loads(policy.read_text())
        data["profiles"].append({"home": "relative", "device": 1, "inode": 1})
        policy.write_text(json.dumps(data))
        assert not mod._full_content_enabled()
        _write_process_policy(tmp_path, home)
        old = home.rename(tmp_path / "original")
        home.mkdir()
        (home / "config.yaml").write_text("plugins: {}\n")
        assert old.stat().st_ino != home.stat().st_ino
        assert not mod._full_content_enabled()
        _write_process_policy(tmp_path, home)
        assert mod._full_content_enabled()
        other_policy = policy.rename(tmp_path / "other-policy")
        policy.symlink_to(other_policy)
        assert not mod._full_content_enabled()
    finally:
        reset_hermes_home_override(token)
