from __future__ import annotations

import os
import json
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from SLQuest_QuestEngine import handle_message


load_dotenv("SLQuest.env")

app = Flask(__name__)

SLQUEST_TOKEN = os.getenv("SLQUEST_TOKEN", "")
SLQUEST_LLM_PROVIDER = os.getenv("SLQUEST_LLM_PROVIDER", "")
PORT = int(os.getenv("PORT", "8000"))

LOGS_ROOT = Path(__file__).resolve().parent / "logs"


def ensure_logs_root() -> None:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)


def sanitize_short(value: str, max_length: int, default: str) -> str:
    if not value:
        return default
    cleaned = re.sub(r"[^A-Za-z0-9]", "_", value)[:max_length]
    return cleaned or default


def redact_token_in_url(url: str) -> str:
    return re.sub(r"([?&]token=)[^&]*", r"\1[REDACTED]", url, flags=re.IGNORECASE)


def build_safe_headers() -> dict[str, str]:
    safe_headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower_key = key.lower()
        if lower_key == "authorization":
            continue
        if "token" in lower_key:
            safe_headers[key] = "[REDACTED]"
        else:
            safe_headers[key] = value
    return safe_headers


def log_request_to_disk(data: dict | None, raw_body: str) -> None:
    try:
        ensure_logs_root()
        now = datetime.utcnow()
        timestamp = now.isoformat(timespec="milliseconds") + "Z"
        date_folder = LOGS_ROOT / now.strftime("%Y-%m-%d")
        date_folder.mkdir(parents=True, exist_ok=True)

        session_id = (data or {}).get("session_id") or ""
        avatar_key = (data or {}).get("avatar_key") or ""
        avatar_name = (data or {}).get("avatar_name") or ""

        session_id_short = sanitize_short(session_id, 16, "nosession")
        avatar_key_short = (avatar_key[:8] if avatar_key else "noavatar")

        filename = f"{now.strftime('%H%M%S')}_{now.strftime('%f')[:3]}_{session_id_short}_{avatar_key_short}.json"
        log_path = date_folder / filename

        request_path = redact_token_in_url(request.full_path)
        if request_path.endswith("?"):
            request_path = request_path[:-1]

        try:
            parsed_json = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            parsed_json = None

        log_entry = {
            "server_timestamp": timestamp,
            "remote_ip": request.remote_addr,
            "request_path": request_path,
            "request_headers": build_safe_headers(),
            "raw_body": raw_body[:2000],
            "parsed_json": parsed_json,
        }

        with log_path.open("w", encoding="utf-8") as handle:
            json.dump(log_entry, handle, ensure_ascii=False, indent=2)

        summary_message = raw_body[:120].replace("\n", " ").replace("\r", " ")
        summary_line = (
            f"[{timestamp}] ip={request.remote_addr} avatar={avatar_name} "
            f"avatar_key={avatar_key} session={session_id_short} msg=\"{summary_message}\""
        )
        summary_path = LOGS_ROOT / "SLQuest_requests.log"
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(summary_line + "\n")
    except Exception:
        return


ensure_logs_root()


@app.get("/health")
def health() -> str:
    return "ok"


@app.post("/slquest")
def slquest() -> tuple:
    raw_body = request.get_data(as_text=True) or ""
    data = request.get_json(silent=True)
    log_request_to_disk(data, raw_body)

    if SLQUEST_TOKEN:
        token = request.args.get("token") or request.headers.get("X-SLQuest-Token")
        if token != SLQUEST_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
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
