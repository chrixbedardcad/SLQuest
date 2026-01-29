"""SLQuest Quest Engine - Dynamic quest system with shared object pool."""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent
LOGS_ROOT = BASE_DIR / "logs"
LOG_PATH = LOGS_ROOT / "quest_engine.log"
POOL_DIR = BASE_DIR / "pools"
POOL_FILE = POOL_DIR / "objects.json"
PLAYER_STATE_DIR = BASE_DIR / "quests" / "player"

# How long an object stays "active" in the shared pool without re-registering.
# In practice, in-world objects may not ping frequently, and dev sessions can have gaps.
# Keep this generous to avoid "no_active_objects" during testing.
POOL_STALE_SECONDS = 7 * 24 * 60 * 60  # 7 days
MAX_RECENT_OBJECTS = 20
DEFAULT_QUEST_COUNT = 2


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_line(line: str) -> None:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _log_event(event: str, avatar_key: str, details: str = "") -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    suffix = f" {details}" if details else ""
    _log_line(f"[{timestamp}] event={event} avatar={avatar_key or '-'}{suffix}")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


# -----------------------------------------------------------------------------
# Pool Management
# -----------------------------------------------------------------------------

def load_pool() -> dict[str, Any]:
    """Load the shared object pool."""
    if not POOL_FILE.exists():
        return {"objects": {}}
    try:
        loaded = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"objects": {}}
    if not isinstance(loaded, dict):
        return {"objects": {}}
    if not isinstance(loaded.get("objects"), dict):
        loaded["objects"] = {}
    return loaded


def get_active_objects(
    min_difficulty: int | None = None,
    max_difficulty: int | None = None,
    category: str | None = None,
    exclude_objects: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get active objects, filtering out stale (>10 min since last_seen)."""
    pool = load_pool()
    objects = pool.get("objects", {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=POOL_STALE_SECONDS)
    exclude_set = set(exclude_objects or [])
    active = []

    for obj_id, obj_data in objects.items():
        if not isinstance(obj_data, dict):
            continue
        if obj_id in exclude_set:
            continue

        last_seen_str = obj_data.get("last_seen")
        if last_seen_str:
            try:
                last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen < cutoff:
                    continue  # Stale
            except (ValueError, TypeError):
                continue  # Invalid timestamp
        else:
            continue  # No timestamp

        # Apply filters
        difficulty = obj_data.get("difficulty", 1)
        if min_difficulty is not None and difficulty < min_difficulty:
            continue
        if max_difficulty is not None and difficulty > max_difficulty:
            continue
        if category is not None and obj_data.get("category") != category:
            continue

        active.append(obj_data)

    return active


# -----------------------------------------------------------------------------
# Player State
# -----------------------------------------------------------------------------

def load_player_state(avatar_key: str) -> dict[str, Any]:
    """Load player state from file."""
    if not avatar_key:
        return {"current_quest": None, "history": {"quests_completed": 0, "recent_objects": []}}

    path = PLAYER_STATE_DIR / f"{avatar_key}.json"
    if not path.exists():
        return {"current_quest": None, "history": {"quests_completed": 0, "recent_objects": []}}

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"current_quest": None, "history": {"quests_completed": 0, "recent_objects": []}}

    if not isinstance(loaded, dict):
        return {"current_quest": None, "history": {"quests_completed": 0, "recent_objects": []}}

    # Ensure structure
    if "history" not in loaded or not isinstance(loaded.get("history"), dict):
        loaded["history"] = {"quests_completed": 0, "recent_objects": []}
    if "quests_completed" not in loaded["history"]:
        loaded["history"]["quests_completed"] = 0
    if "recent_objects" not in loaded["history"]:
        loaded["history"]["recent_objects"] = []

    return loaded


def save_player_state(avatar_key: str, state: dict[str, Any]) -> None:
    """Save player state to file."""
    if not avatar_key:
        return
    path = PLAYER_STATE_DIR / f"{avatar_key}.json"
    atomic_write_json(path, state)


# -----------------------------------------------------------------------------
# Quest Generation
# -----------------------------------------------------------------------------

def generate_quest(
    avatar_key: str,
    difficulty: int | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """Generate a quest from the shared pool.

    Auto-scales count based on player history if not specified.
    Returns {ok, quest_id, objectives, ...} or {ok: false, error}.
    """
    if not avatar_key:
        return {"ok": False, "error": "missing_avatar_key"}

    state = load_player_state(avatar_key)

    # Check if player already has an active quest
    current = state.get("current_quest")
    if current and current.get("status") == "active":
        _log_event("generate_quest_skipped", avatar_key, "already_has_active_quest")
        return {
            "ok": False,
            "error": "already_has_active_quest",
            "quest_id": current.get("quest_id"),
        }

    history = state.get("history", {})
    quests_completed = history.get("quests_completed", 0)
    recent_objects = history.get("recent_objects", [])

    # Auto-scale count if not specified
    if count is None:
        count = min(quests_completed + 1, 3)
        count = max(1, count)

    # Get available objects
    active_objects = get_active_objects(
        min_difficulty=difficulty,
        max_difficulty=difficulty,
        exclude_objects=recent_objects,
    )

    if len(active_objects) < count:
        # Try without difficulty filter
        active_objects = get_active_objects(exclude_objects=recent_objects)

    if len(active_objects) < count:
        # Try without excluding recent objects
        active_objects = get_active_objects()

    if not active_objects:
        _log_event("generate_quest_failed", avatar_key, "no_active_objects")
        return {"ok": False, "error": "no_active_objects"}

    # Random select
    selected = random.sample(active_objects, min(count, len(active_objects)))

    # Build objectives
    objectives = []
    for obj in selected:
        objectives.append({
            "object_id": obj.get("object_id"),
            "object_name": obj.get("object_name", obj.get("object_id", "")),
            "hint": obj.get("hint", ""),
            "found": False,
            "found_at": None,
        })

    # Generate quest
    quest_id = f"quest_{uuid4().hex[:12]}"
    quest_difficulty = difficulty if difficulty else (
        max(obj.get("difficulty", 1) for obj in selected)
    )

    new_quest = {
        "quest_id": quest_id,
        "difficulty": quest_difficulty,
        "generated_at": _now_iso(),
        "objectives": objectives,
        "status": "active",
        "reward_given": False,
    }

    state["current_quest"] = new_quest
    save_player_state(avatar_key, state)

    _log_event(
        "generate_quest_ok",
        avatar_key,
        f"quest_id={quest_id} objectives={len(objectives)} difficulty={quest_difficulty}",
    )

    return {
        "ok": True,
        "quest_id": quest_id,
        "difficulty": quest_difficulty,
        "objectives": objectives,
        "count": len(objectives),
    }


# -----------------------------------------------------------------------------
# Event Handling
# -----------------------------------------------------------------------------

def handle_quest_event(avatar_key: str, object_id: str) -> dict[str, Any]:
    """Handle object_found event.

    Returns {matched, quest_completed, found_count, total_count, ...}
    """
    if not avatar_key or not object_id:
        return {"ok": False, "matched": False, "error": "missing_fields"}

    state = load_player_state(avatar_key)
    current = state.get("current_quest")

    if not current or current.get("status") != "active":
        _log_event("quest_event_no_quest", avatar_key, f"object_id={object_id}")
        return {"ok": True, "matched": False, "reason": "no_active_quest"}

    objectives = current.get("objectives", [])
    objective_ids = [o.get("object_id") for o in objectives]
    _log_event("quest_event_received", avatar_key, f"clicked={object_id} quest_objectives={objective_ids}")

    matched = False
    found_count = 0
    total_count = len(objectives)

    for obj in objectives:
        if obj.get("object_id") == object_id:
            if not obj.get("found"):
                obj["found"] = True
                obj["found_at"] = _now_iso()
                matched = True
                _log_event(
                    "quest_event_found",
                    avatar_key,
                    f"quest_id={current.get('quest_id')} object_id={object_id}",
                )
        if obj.get("found"):
            found_count += 1

    # Check completion
    quest_completed = found_count >= total_count
    if quest_completed and current.get("status") != "completed":
        current["status"] = "completed"
        current["completed_at"] = _now_iso()
        _log_event(
            "quest_completed",
            avatar_key,
            f"quest_id={current.get('quest_id')} found={found_count}/{total_count}",
        )

    save_player_state(avatar_key, state)

    return {
        "ok": True,
        "matched": matched,
        "quest_completed": quest_completed,
        "found_count": found_count,
        "total_count": total_count,
        "quest_id": current.get("quest_id"),
    }


# -----------------------------------------------------------------------------
# AI Integration
# -----------------------------------------------------------------------------

def build_quest_context(avatar_key: str) -> str:
    """Build context string for LLM."""
    state = load_player_state(avatar_key)
    current = state.get("current_quest")
    history = state.get("history", {})

    if not current:
        lines = [
            "QUEST_STATUS:",
            "status=none",
            f"quests_completed={history.get('quests_completed', 0)}",
            "",
            "QUEST_RULES:",
            "- Player has no active quest",
            "- If player wants a quest, generate one using generate_quest()",
            "- Offer to give them an adventure/quest if they seem interested",
        ]
        return "\n".join(lines)

    objectives = current.get("objectives", [])
    found_count = sum(1 for obj in objectives if obj.get("found"))
    total_count = len(objectives)
    status = current.get("status", "active")
    reward_given = current.get("reward_given", False)

    # Separate found and unfound objectives
    found_objects = []
    unfound_objects = []
    for obj in objectives:
        obj_info = {
            "object_id": obj.get("object_id", ""),
            "object_name": obj.get("object_name", obj.get("object_id", "")),
            "hint": obj.get("hint", ""),
        }
        if obj.get("found"):
            found_objects.append(obj_info)
        else:
            unfound_objects.append(obj_info)

    lines = [
        "QUEST_STATUS:",
        f"quest_id={current.get('quest_id', '')}",
        f"status={status}",
        f"difficulty={current.get('difficulty', 1)}",
        f"found={found_count}/{total_count}",
    ]

    # List found objects so NPC knows what player already found
    if found_objects:
        lines.append("FOUND_OBJECTS:")
        for obj in found_objects:
            obj_name = obj.get("object_name", obj.get("object_id", "unknown"))
            lines.append(f"- {obj_name} (FOUND)")

    # List unfound objects so NPC knows what to tell player
    if unfound_objects and status == "active":
        lines.append("REMAINING_OBJECTIVES:")
        for i, obj in enumerate(unfound_objects):
            obj_id = obj.get("object_id", "unknown")
            obj_name = obj.get("object_name", obj_id)
            hint = obj.get("hint", "")
            lines.append(f"- {obj_name}: hint=\"{hint}\"")

    if reward_given:
        lines.append("reward_given=true")

    lines.append("")
    lines.append("QUEST_RULES:")

    if status == "active":
        lines.extend([
            "- Quest is active, player is searching for objects",
            "- FOUND_OBJECTS shows what player already found - acknowledge these",
            "- REMAINING_OBJECTIVES shows what player still needs to find",
            "- Use the hints provided to help guide them to remaining objects",
            "- Never claim an object is found unless it's listed under FOUND_OBJECTS",
            f"- Player has found {found_count} of {total_count} objects",
        ])
    elif status == "completed" and not reward_given:
        lines.extend([
            "- All objects found! Quest is complete",
            "- Congratulate the player warmly",
            "- A reward will be given automatically",
            "- After reward, offer another quest if they want",
        ])
    elif status == "completed" and reward_given:
        lines.extend([
            "- Quest completed and reward given",
            "- Offer another quest if player is interested",
            "- Player can request a new adventure anytime",
        ])

    lines.append("- Keep replies short, single message, no markdown")

    context = "\n".join(lines)
    _log_event("build_context", avatar_key, f"context_preview={context[:200].replace(chr(10), '|')}")
    return context


def quest_pre_chat(avatar_key: str, npc_id: str, message: str) -> dict[str, Any]:
    """Pre-chat hook. Returns {quest_context, actions}."""
    state = load_player_state(avatar_key)
    current = state.get("current_quest")
    actions: list[str] = []

    # Detect quest request keywords
    lower_msg = message.lower()
    wants_quest = any(kw in lower_msg for kw in [
        # Direct intent
        "quest", "adventure", "mission", "task", "something to do", "bored", "challenge",
        # Typical quest verbs
        "find", "hunt", "search",
        # When NPC offers a quest, users often answer like this
        "sure", "yes", "yeah", "yep", "ok", "okay", "of course", "why not",
        "let's go", "lets go", "let's do it", "lets do it",
        "give me", "start", "begin", "ready", "i want", "i'd like", "i would like",
        # If NPC asks for a style choice ("spooky or chill"), treat the choice as acceptance
        "chill", "spooky",
    ])

    # Auto-generate if no quest and player seems to want one
    if not current and wants_quest:
        result = generate_quest(avatar_key)
        if result.get("ok"):
            _log_event(
                "quest_auto_generated",
                avatar_key,
                f"quest_id={result.get('quest_id')} npc_id={npc_id}",
            )

    quest_context = build_quest_context(avatar_key)

    _log_event(
        "quest_pre_chat",
        avatar_key,
        f"npc_id={npc_id} has_quest={bool(current)} wants_quest={wants_quest}",
    )

    return {"quest_context": quest_context, "actions": actions}


def quest_post_chat(avatar_key: str, npc_id: str, message: str) -> dict[str, Any]:
    """Post-chat hook. Handles rewards. Returns {actions}."""
    state = load_player_state(avatar_key)
    current = state.get("current_quest")
    actions: list[str] = []

    if current and current.get("status") == "completed":
        if not current.get("reward_given"):
            # Give reward - use placeholder, API will replace with actual gift
            actions.append("Give:QUEST_REWARD")
            current["reward_given"] = True
            current["reward_given_at"] = _now_iso()

            # Update history
            history = state.setdefault("history", {"quests_completed": 0, "recent_objects": []})
            history["quests_completed"] = history.get("quests_completed", 0) + 1

            for obj in current.get("objectives", []):
                obj_id = obj.get("object_id")
                if obj_id and obj_id not in history["recent_objects"]:
                    history["recent_objects"].append(obj_id)

            # Keep only last N
            history["recent_objects"] = history["recent_objects"][-MAX_RECENT_OBJECTS:]

            # Clear current quest
            state["current_quest"] = None
            save_player_state(avatar_key, state)

            _log_event(
                "quest_reward_given",
                avatar_key,
                f"npc_id={npc_id} total_completed={history['quests_completed']}",
            )

    return {"actions": actions}


# -----------------------------------------------------------------------------
# Legacy compatibility - these are called by the old API
# -----------------------------------------------------------------------------

def load_definition(quest_id: str) -> dict[str, Any]:
    """Legacy: Load quest definition (now returns empty for dynamic quests)."""
    return {}


def quest_handle_event(
    avatar_key: str, quest_id: str, event: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Legacy: Handle quest event (redirects to new system)."""
    meta = meta or {}
    object_id = meta.get("object_id") or meta.get("object_key") or ""

    if event == "object_found":
        return handle_quest_event(avatar_key, object_id)

    # Legacy cube_clicked event
    if event == "cube_clicked":
        return handle_quest_event(avatar_key, object_id)

    _log_event("quest_event_unknown", avatar_key, f"event={event}")
    return {"ok": True, "matched": False}
