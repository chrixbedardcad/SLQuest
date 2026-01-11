from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from openai import OpenAI


load_dotenv("SLQuest.env")

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8001"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

LOGS_ROOT = Path(__file__).resolve().parent / "logs"
CHAT_ROOT = Path(__file__).resolve().parent / "chat"
RUN_LOG_PATH = LOGS_ROOT / f"SLQuest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"

CLIENT = OpenAI()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def log_request_line(endpoint: str, avatar_key: str, message: str, status: str) -> None:
    ensure_dir(LOGS_ROOT)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snippet = message.replace("\n", " ").replace("\r", " ")[:120]
    line = (
        f"[{timestamp}] endpoint={endpoint} avatar_key={avatar_key or '-'} "
        f"status={status} msg=\"{snippet}\""
    )
    with RUN_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def history_path(avatar_key: str) -> Path:
    safe_key = avatar_key or "unknown"
    avatar_dir = CHAT_ROOT / safe_key
    ensure_dir(avatar_dir)
    return avatar_dir / f"chatgpt_histo_{safe_key}.json"


def load_history(avatar_key: str, last_n: int) -> list[dict[str, Any]]:
    path = history_path(avatar_key)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data[-last_n:]


def append_history(avatar_key: str, events: list[dict[str, Any]]) -> None:
    path = history_path(avatar_key)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except json.JSONDecodeError:
            existing = []
    existing.extend(events)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def trim_to_bytes(text: str, max_bytes: int) -> str:
    total = 0
    end_index = 0
    for index, char in enumerate(text):
        char_bytes = len(char.encode("utf-8"))
        if total + char_bytes > max_bytes:
            break
        total += char_bytes
        end_index = index + 1
    return text[:end_index]


def clamp_reply(text: str, max_bytes: int = 1024) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    trimmed = trim_to_bytes(text, max_bytes)
    boundary = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
    if boundary != -1:
        return trimmed[: boundary + 1]

    ellipsis = "…"
    fallback = trim_to_bytes(text, max_bytes - len(ellipsis.encode("utf-8")))
    return fallback + ellipsis


def build_instructions(npc_id: str) -> str:
    return (
        "You are an SLQuest NPC chatting in Second Life. "
        f"NPC ID: {npc_id}. "
        "Reply must be short for Second Life: aim <= 900 characters. "
        "No markdown. One message only."
    )


def build_messages(history: list[dict[str, Any]], message: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for event in history:
        role = event.get("role")
        content = event.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


@app.get("/health")
def health() -> tuple[str, int]:
    log_request_line("/health", "", "", "200")
    return "ok", 200


@app.post("/chat")
def chat() -> tuple:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        reply = "Sorry, I glitched. Try again."
        log_request_line("/chat", "", "", "400")
        return jsonify({"ok": False, "reply": reply, "error": "invalid_json"}), 400

    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    object_key = (data.get("object_key") or "").strip()
    region = (data.get("region") or "").strip()
    timestamp = (data.get("ts") or datetime.now(timezone.utc).isoformat())

    if not message:
        reply = "Sorry, I glitched. Try again."
        log_request_line("/chat", avatar_key, message, "400")
        return jsonify({"ok": False, "reply": reply, "error": "message_required"}), 400

    history = load_history(avatar_key, last_n=8)
    messages = build_messages(history, message)
    instructions = build_instructions(npc_id)

    reply_text = ""
    error_message = ""

    try:
        response = CLIENT.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=messages,
        )
        reply_text = (response.output_text or "").strip()
        if not reply_text:
            error_message = "empty_reply"
    except Exception as exc:
        error_message = str(exc)[:120]

    if error_message:
        reply_text = "Sorry, I glitched. Try again."
        ok = False
    else:
        ok = True

    reply_text = clamp_reply(reply_text)

    user_event = {
        "ts": timestamp,
        "role": "user",
        "content": message,
        "npc_id": npc_id,
        "object_key": object_key,
        "region": region,
    }
    assistant_event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": "assistant",
        "content": reply_text,
        "npc_id": npc_id,
        "object_key": object_key,
        "region": region,
    }
    append_history(avatar_key, [user_event, assistant_event])

    status_code = 200 if ok else 502
    log_request_line("/chat", avatar_key, message, str(status_code))

    response_payload: dict[str, Any] = {
        "ok": ok,
        "reply": reply_text,
        "reply_chars": len(reply_text),
    }
    if not ok:
        response_payload["error"] = error_message

    return jsonify(response_payload), status_code


if __name__ == "__main__":
    from waitress import serve

    ensure_dir(LOGS_ROOT)
    ensure_dir(CHAT_ROOT)
    serve(app, host="0.0.0.0", port=PORT)
