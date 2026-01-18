from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from enrich.profile_enricher import get_or_create_profile_card

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, "SLQuest.env"))

app = Flask(__name__)

PORT = int(os.getenv("PROFILE_ENRICHER_PORT", "8002"))


def json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


@app.post("/profile/enrich")
def profile_enrich():
    payload = request.get_json(silent=True) or {}
    avatar_uuid = (payload.get("avatar_uuid") or "").strip()
    force = bool(payload.get("force"))
    if not avatar_uuid:
        return json_error("avatar_uuid_required", 400)
    try:
        card = get_or_create_profile_card(avatar_uuid, force=force)
    except Exception as exc:
        return json_error(f"enrichment_failed: {exc}", 500)
    return jsonify(card)


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "profile_enricher",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
