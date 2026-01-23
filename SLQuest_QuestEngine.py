from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
QUEST_DEFINITIONS_DIR = BASE_DIR / "quests" / "definitions"
QUEST_STATE_DIR = BASE_DIR / "quests" / "state"

DEFAULT_QUEST_ID = "find_green_cube"


def _now_ts() -> int:
    return int(time.time())


def load_definition(quest_id: str) -> dict[str, Any]:
    path = QUEST_DEFINITIONS_DIR / f"{quest_id}.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_player_state(avatar_key: str) -> dict[str, Any]:
    if not avatar_key:
        return {"quests": {}}
    path = QUEST_STATE_DIR / f"{avatar_key}.json"
    if not path.exists():
        return {"quests": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"quests": {}}
    if not isinstance(loaded, dict):
        return {"quests": {}}
    quests = loaded.get("quests")
    if not isinstance(quests, dict):
        loaded["quests"] = {}
    return loaded


def save_player_state(avatar_key: str, state: dict[str, Any]) -> None:
    if not avatar_key:
        return
    QUEST_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEST_STATE_DIR / f"{avatar_key}.json"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _ensure_quest_entry(
    state: dict[str, Any], quest_id: str, npc_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    quests = state.setdefault("quests", {})
    changed = False
    quest = quests.get(quest_id)
    if not isinstance(quest, dict):
        quest = {}
        quests[quest_id] = quest
        changed = True
    if quest.get("state") not in ("started", "clicked", "completed"):
        quest["state"] = "started"
        changed = True
    if npc_id and quest.get("npc_id") != npc_id:
        quest["npc_id"] = npc_id
        changed = True
    if "started_at" not in quest:
        quest["started_at"] = _now_ts()
        changed = True
    for key in ("clicked_at", "clicked_object_key", "completed_at", "reward_given_at"):
        if key not in quest:
            quest[key] = None
            changed = True
    return quest, changed


def build_quest_context(avatar_key: str, npc_id: str) -> str:
    definition = load_definition(DEFAULT_QUEST_ID)
    state = load_player_state(avatar_key)
    quest = state.get("quests", {}).get(DEFAULT_QUEST_ID, {})
    state_value = quest.get("state")
    started = state_value in ("started", "clicked", "completed")
    clicked = state_value in ("clicked", "completed")
    completed = state_value == "completed"
    reward_given = bool(quest.get("reward_given_at"))

    llm_context = definition.get("llm_context", {}) if isinstance(definition, dict) else {}
    goal = llm_context.get("goal") or ""
    step_logic = llm_context.get("step_logic") or []
    hard_rules = llm_context.get("hard_rules") or []
    step_logic_text = " | ".join([str(item) for item in step_logic if item])
    hard_rules_text = " | ".join([str(item) for item in hard_rules if item])

    lines = [
        f"quest_id={DEFAULT_QUEST_ID}",
        f"started={'true' if started else 'false'}",
        f"clicked={'true' if clicked else 'false'}",
        f"completed={'true' if completed else 'false'}",
        f"reward_given={'true' if reward_given else 'false'}",
    ]
    if goal:
        lines.append(f"goal={goal}")
    if step_logic_text:
        lines.append(f"step_logic={step_logic_text}")
    if hard_rules_text:
        lines.append(f"hard_rules={hard_rules_text}")
    lines.append(
        "llm_rules=Treat QUEST_CONTEXT as truth; never invent clicks or rewards; keep reply short and single-line."
    )
    return "\n".join(lines)


def quest_pre_chat(avatar_key: str, npc_id: str, raw_message: str) -> dict[str, Any]:
    state = load_player_state(avatar_key)
    quest, changed = _ensure_quest_entry(state, DEFAULT_QUEST_ID, npc_id=npc_id)
    if changed:
        save_player_state(avatar_key, state)
    quest_context = build_quest_context(avatar_key, npc_id)
    quest_pack = {"quest_id": DEFAULT_QUEST_ID, "state": quest.get("state", "")}
    return {"quest_context": quest_context, "quest": quest_pack, "actions": []}


def quest_post_chat(avatar_key: str, npc_id: str, raw_message: str) -> dict[str, Any]:
    state = load_player_state(avatar_key)
    quest = state.get("quests", {}).get(DEFAULT_QUEST_ID, {})
    actions: list[str] = []
    changed = False
    if isinstance(quest, dict) and quest.get("state") == "clicked":
        quest["state"] = "completed"
        quest["completed_at"] = _now_ts()
        changed = True
        if not quest.get("reward_given_at"):
            quest["reward_given_at"] = _now_ts()
            actions.append("Give:Green Cube Prize")
    if changed:
        save_player_state(avatar_key, state)
    quest_pack = {"quest_id": DEFAULT_QUEST_ID, "state": quest.get("state", "")}
    return {"quest": quest_pack, "actions": actions}


def quest_handle_event(
    avatar_key: str, quest_id: str, event: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    if quest_id != DEFAULT_QUEST_ID or event != "cube_clicked":
        return {"ok": True}
    meta = meta or {}
    state = load_player_state(avatar_key)
    quest, changed = _ensure_quest_entry(state, DEFAULT_QUEST_ID)
    if quest.get("state") != "completed":
        if quest.get("state") != "clicked":
            quest["state"] = "clicked"
            changed = True
        if not quest.get("clicked_at"):
            quest["clicked_at"] = _now_ts()
            changed = True
        object_key = meta.get("object_key")
        if object_key and quest.get("clicked_object_key") != object_key:
            quest["clicked_object_key"] = object_key
            changed = True
    if changed:
        save_player_state(avatar_key, state)
    return {"ok": True}
