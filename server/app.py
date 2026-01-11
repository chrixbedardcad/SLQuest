from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from flask import Flask, jsonify, request

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8000"))
SL_TOKEN = os.getenv("SL_TOKEN", "CHANGE_ME")

SESSIONS: Dict[str, str] = {}


def _is_authorized(req_token: str | None) -> bool:
    if not req_token:
        return False
    return req_token == SL_TOKEN


def _extract_token() -> str | None:
    header_token = request.headers.get("X-SL-Token")
    query_token = request.args.get("token")
    return header_token or query_token


def _log_request(payload: Dict[str, Any]) -> None:
    avatar_name = payload.get("avatar_name", "")
    avatar_key = payload.get("avatar_key", "")
    session_id = payload.get("session_id", "")
    message = payload.get("message", "")
    if isinstance(message, str):
        message = message[:120]
    else:
        message = str(message)[:120]
    timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    print(
        f"{timestamp} | {avatar_name} | {avatar_key} | {session_id} | {message}",
        flush=True,
    )


@app.get("/health")
def health() -> tuple[str, int]:
    return "ok", 200


@app.post("/sl")
def sl() -> tuple[Any, int]:
    token = _extract_token()
    if not _is_authorized(token):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_json"}), 400

    _log_request(payload)

    message = str(payload.get("message", ""))
    session_id = str(payload.get("session_id", ""))

    state = SESSIONS.get(session_id, "none")
    reply = ""
    quest = {"state": state}

    if "start" in message.lower():
        state = "intro"
        SESSIONS[session_id] = state
        reply = "Quest started. Find the color blue."
        quest = {"state": state, "hint": "Say something with the word blue."}
    elif state == "intro" and "blue" in message.lower():
        state = "complete"
        SESSIONS[session_id] = state
        reply = "Quest complete. Nice work!"
        quest = {"state": state}
    else:
        if state == "intro":
            reply = "Hint: try saying the color blue."
            quest = {"state": state, "hint": "Say something with the word blue."}
        elif state == "complete":
            reply = "You already completed the quest."
            quest = {"state": state}
        else:
            reply = "Say 'start' to begin your quest."
            quest = {"state": "intro", "hint": "Touch to start or say start."}

    response = {
        "reply": reply,
        "session_id": session_id,
        "quest": quest,
    }
    return jsonify(response), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
