from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv(os.path.join(BASE_DIR, "SLQuest.env"))

if __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrich.profile_enricher import get_or_create_profile_card, log_line

app = Flask(__name__)

PORT = int(os.getenv("PROFILE_ENRICHER_PORT", "8002"))
MAX_WORKERS = int(os.getenv("PROFILE_ENRICHER_WORKERS", "3"))

EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)
IN_FLIGHT: set[str] = set()
IN_FLIGHT_LOCK = Lock()


def json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


@app.post("/profile/enrich")
def profile_enrich():
    payload = request.get_json(silent=True) or {}
    avatar_uuid = (payload.get("avatar_uuid") or "").strip()
    avatar_name = (payload.get("avatar_name") or "").strip()
    avatar_display_name = (payload.get("avatar_display_name") or "").strip()
    avatar_username = (payload.get("avatar_username") or "").strip()
    force = bool(payload.get("force"))
    if not avatar_uuid:
        log_line("profile_enrich_invalid request=missing_avatar_uuid")
        return json_error("avatar_uuid_required", 400)
    with IN_FLIGHT_LOCK:
        if avatar_uuid in IN_FLIGHT:
            log_line(f"profile_enrich_inflight avatar={avatar_uuid}")
            return jsonify({"ok": True, "queued": True}), 202
        IN_FLIGHT.add(avatar_uuid)

    def run_job() -> None:
        start_time = time.perf_counter()
        try:
            get_or_create_profile_card(
                avatar_uuid,
                force=force,
                avatar_name=avatar_name,
                avatar_display_name=avatar_display_name,
                avatar_username=avatar_username,
            )
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            log_line(
                f"profile_enrich_done avatar={avatar_uuid} force={int(force)} elapsed_ms={elapsed_ms}"
            )
        except Exception as exc:
            log_line(f"profile_enrich_failed avatar={avatar_uuid} error={exc}")
        finally:
            with IN_FLIGHT_LOCK:
                IN_FLIGHT.discard(avatar_uuid)

    EXECUTOR.submit(run_job)
    log_line(f"profile_enrich_queued avatar={avatar_uuid} force={int(force)}")
    return jsonify({"ok": True, "queued": True}), 202


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
