from __future__ import annotations

from typing import Dict

_QUEST_STATE: Dict[str, str] = {}


def handle_message(session_id: str, message: str) -> dict:
    """Handle a quest message and return reply + quest state."""
    normalized = message.lower()
    state = _QUEST_STATE.get(session_id, "idle")

    if "start" in normalized:
        state = "intro"
        _QUEST_STATE[session_id] = state
        return {
            "reply": "Quest started! Tell me a color. Hint: the sky holds the keyword.",
            "quest": {
                "state": state,
                "hint": "Say the color of a clear daytime sky.",
            },
        }

    if state == "intro" and "blue" in normalized:
        state = "complete"
        _QUEST_STATE[session_id] = state
        return {
            "reply": "Quest complete!",
            "quest": {
                "state": state,
                "reward": "You earned: Blue Feather",
            },
        }

    if state == "intro":
        return {
            "reply": "Keep going. Name the sky color to finish.",
            "quest": {
                "state": state,
                "hint": "It rhymes with 'glue'.",
            },
        }

    return {
        "reply": "Say 'start' to begin the quest.",
        "quest": {
            "state": state,
            "hint": "Touch the object or say 'start'.",
        },
    }
