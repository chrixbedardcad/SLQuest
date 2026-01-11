from __future__ import annotations

import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from SLQuest_QuestEngine import handle_message


ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(ENV_PATH)

app = Flask(__name__)

SLQUEST_TOKEN = os.getenv("SLQUEST_TOKEN", "")
SLQUEST_LLM_PROVIDER = os.getenv("SLQUEST_LLM_PROVIDER", "")
PORT = int(os.getenv("PORT", "8000"))


@app.get("/health")
def health() -> str:
    return "ok"


@app.post("/slquest")
def slquest() -> tuple:
    if SLQUEST_TOKEN:
        token = request.args.get("token") or request.headers.get("X-SLQuest-Token")
        if token != SLQUEST_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid_json"}), 400

    message = (data.get("message") or "").strip()
    session_id = (data.get("session_id") or "").strip()

    if not message:
        return jsonify({"error": "message_required"}), 400
    if not session_id:
        return jsonify({"error": "session_id_required"}), 400

    message = message[:500]

    timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    avatar_name = data.get("avatar_name", "")
    avatar_key = data.get("avatar_key", "")
    log_message = message[:120]
    print(
        f"{timestamp} avatar_name={avatar_name} avatar_key={avatar_key} "
        f"session_id={session_id} message={log_message}"
    )

    result = handle_message(session_id, message)
    response = {
        "reply": result["reply"],
        "session_id": session_id,
        "quest": result["quest"],
    }
    return jsonify(response), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
