"""Validated operator policy for unrestricted gateway turns; no prompt classification."""
from typing import Any


_FIELDS = {"policy_revision", "operation", "reason", "model", "provider"}


def runtime_model_identity(model: str, provider: str) -> str:
    """Use the same native-provider normalization as AIAgent construction."""
    from hermes_cli.model_normalize import _AGGREGATOR_PROVIDERS, normalize_model_for_provider

    if provider and provider not in _AGGREGATOR_PROVIDERS:
        return normalize_model_for_provider(model, provider)
    return model


def select_turn_model(policy: Any, model: str, provider: str) -> tuple[str, dict | None]:
    """Keep provider credentials scoped to the resolved runtime; reject bad policy.

    Future bounded operations need a real native caller with trusted provenance
    before adding a selector here. User text never establishes that provenance.
    """
    if policy is None:
        return model, None
    receipt = {
        "status": "invalid_policy", "reason": "invalid turn_model_selection configuration",
        "operation": "unrestricted_turn", "operation_provenance": "gateway",
        "selected_model": model, "selected_provider": provider,
    }
    if isinstance(policy, dict) and isinstance(policy.get("policy_revision"), str):
        receipt["policy_revision"] = policy["policy_revision"]
    if (not isinstance(policy, dict) or set(policy) != _FIELDS
            or any(not isinstance(policy[k], str) or not policy[k].strip() for k in _FIELDS)
            or policy["operation"] != "unrestricted_turn"
            or policy["provider"] != provider):
        return model, receipt
    receipt.update(
        status="selected", policy_revision=policy["policy_revision"], reason=policy["reason"],
        selected_model=policy["model"], selected_provider=policy["provider"],
    )
    return policy["model"], receipt
