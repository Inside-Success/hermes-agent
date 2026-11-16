"""User-initiated edit/delete for journey nodes (learned skills + memories).

The journey graph (``agent.learning_graph``) gives every node a stable id:

- **skills** → the skill name (e.g. ``"debugging-hermes-desktop"``)
- **memories** → ``memory:<source>:<profile-scoped item UUID>:<expected revision UUID>``.
  The item UUID survives edits; the expected revision rejects stale mutations.
  Positional handles from older versions cannot authorize writes.

This module maps a node id back to its on-disk home and performs the mutation,
shared by the CLI (``hermes journey delete|edit``), the TUI ``/journey`` overlay
(gateway RPCs), and the desktop GUI (REST). Deleting a skill *archives* it
(recoverable via ``hermes curator restore``); deleting a memory rewrites its
file. Pure stdlib + existing skill/memory helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def parse_node_kind(node_id: str) -> str:
    return "memory" if node_id.startswith("memory:") else "skill"


def _memory_store():
    from tools.memory_tool import load_on_disk_store
    return load_on_disk_store()


def _memory_item(node_id):
    store = _memory_store()
    parts = store._parse_node_id(node_id)
    target = 'user' if parts[1] == 'profile' else 'memory'
    matches = [item for item in store.native_items(target) if item['id'] == node_id]
    if len(matches) != 1:
        with store._native_db() as db:
            history = db.memory_history(target, parts[2])
        if history and history[-1].get('deleted') and history[-1]['revision'] == parts[3]:
            return store, target, dict(history[-1], id=node_id, body='[deleted memory]')
        raise ValueError('Stale memory revision; refresh the journey')
    return store, target, matches[0]


# ── Inspect (edit prefill) ──────────────────────────────────────────────────


def node_detail(node_id: str) -> dict[str, Any]:
    """Current content for an edit prefill. ``content`` is the full SKILL.md
    (skills) or the raw memory chunk (memories)."""
    try:
        return _node_detail(node_id)
    except (ValueError, IndexError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}


def _node_detail(node_id: str) -> dict[str, Any]:
    if parse_node_kind(node_id) == "memory":
        store, target, item = _memory_item(node_id)
        with store._native_db() as db:
            history = db.memory_history(target, item['item_id'])
        return dict(item, ok=True, kind='memory', label=item['body'].splitlines()[0][:80],
                    content=item['body'], history=history)

    from tools.skill_manager_tool import _find_skill

    found = _find_skill(node_id)
    if not found:
        return {"ok": False, "message": f"skill '{node_id}' not found"}
    skill_md = Path(found["path"]) / "SKILL.md"
    if not skill_md.exists():
        return {"ok": False, "message": f"SKILL.md missing for '{node_id}'"}

    return {
        "ok": True,
        "kind": "skill",
        "id": node_id,
        "label": node_id,
        "content": skill_md.read_text(encoding="utf-8"),
    }


# ── Delete ──────────────────────────────────────────────────────────────────


def delete_node(node_id: str) -> dict[str, Any]:
    try:
        return _delete_memory(node_id) if parse_node_kind(node_id) == "memory" else _delete_skill(node_id)
    except (ValueError, IndexError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}


def _delete_skill(name: str) -> dict[str, Any]:
    from tools import skill_usage

    if skill_usage.get_record(name).get("pinned"):
        return {"ok": False, "message": f"'{name}' is pinned — unpin it first (hermes curator unpin {name})"}

    ok, message = skill_usage.archive_skill(name)
    if ok:
        _clear_skill_cache()

    return {"ok": ok, "message": f"archived '{name}' — restore with: hermes curator restore {name}" if ok else message}


def _delete_memory(node_id: str) -> dict[str, Any]:
    return _memory_store().mutate_item(node_id)


# ── Edit ────────────────────────────────────────────────────────────────────


def edit_node(node_id: str, content: str) -> dict[str, Any]:
    try:
        return _edit_memory(node_id, content) if parse_node_kind(node_id) == "memory" else _edit_skill(node_id, content)
    except (ValueError, IndexError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}


def _edit_skill(name: str, content: str) -> dict[str, Any]:
    from tools.skill_manager_tool import _edit_skill as _do_edit

    result = _do_edit(name, content)
    if result.get("success"):
        _clear_skill_cache()

        return {"ok": True, "message": f"updated '{name}'"}

    return {"ok": False, "message": result.get("error", "edit failed")}


def _edit_memory(node_id: str, content: str) -> dict[str, Any]:
    return _memory_store().mutate_item(node_id, content=content)


def revert_node(node_id: str, revision: str) -> dict[str, Any]:
    """Compensate one item's revision while preserving all other current entries."""
    try:
        if parse_node_kind(node_id) != 'memory':
            raise ValueError('Revert supports native memories only')
        return _memory_store().mutate_item(node_id, revert_revision=revision)
    except (ValueError, IndexError, RuntimeError) as exc:
        return {'ok': False, 'message': str(exc)}


def _clear_skill_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
