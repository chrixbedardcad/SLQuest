from __future__ import annotations

import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, request
import openai as openai_pkg
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "SLQuest.env")

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8001"))
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5.2").strip() or "gpt-5.2"

LOGS_ROOT = BASE_DIR / "logs"
CHAT_ROOT = BASE_DIR / "chat"
RUN_LOG_PATH = LOGS_ROOT / f"SLQuest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
ERROR_LOG_PATH = LOGS_ROOT / "SLQuest_errors.log"

CLIENT = OpenAI(api_key=OPENAI_API_KEY)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def log_line(path: Path, line: str) -> None:
    ensure_dir(LOGS_ROOT)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_request_line(
    endpoint: str,
    request_id: str,
    client_req_id: str,
    avatar_key: str,
    npc_id: str,
    message: str,
    status: str,
    elapsed_ms: int,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snippet = message.replace("\n", " ").replace("\r", " ")[:120]
    line = (
        f"[{timestamp}] endpoint={endpoint} request_id={request_id} "
        f"client_req_id={client_req_id or '-'} "
        f"avatar_key={avatar_key or '-'} npc_id={npc_id or '-'} "
        f"status={status} elapsed_ms={elapsed_ms} msg=\"{snippet}\""
    )
    log_line(RUN_LOG_PATH, line)


def log_error(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    log_line(RUN_LOG_PATH, line)
    log_line(ERROR_LOG_PATH, line)


def redact_secrets(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9]+", "sk-***", text)


def safe_error_reason(exc: Exception) -> str:
    reason = f"{type(exc).__name__}: {exc}"
    reason = redact_secrets(reason)
    return reason[:160]


def log_openai_exception(request_id: str, exc: Exception) -> None:
    trace = traceback.format_exc(limit=3)
    message = redact_secrets(str(exc))
    log_error(
        "OpenAI exception "
        f"request_id={request_id} type={type(exc).__name__} "
        f"message=\"{message}\" trace=\"{trace.strip()}\""
    )


def log_unhandled_exception(request_id: str, exc: Exception) -> None:
    trace = traceback.format_exc(limit=3)
    message = redact_secrets(str(exc))
    log_error(
        "Unhandled exception "
        f"request_id={request_id} type={type(exc).__name__} "
        f"message=\"{message}\" trace=\"{trace.strip()}\""
    )


log_line(RUN_LOG_PATH, f"OpenAI SDK version: {openai_pkg.__version__}")

if not OPENAI_API_KEY:
    ensure_dir(LOGS_ROOT)
    startup_message = "ERROR: OPENAI_API_KEY missing. Update SLQuest.env and restart."
    log_line(RUN_LOG_PATH, startup_message)
    print(startup_message)


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
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    status = 200
    response = ("ok", status)
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line("/health", request_id, "", "", "", "", str(status), elapsed_ms)
    return response


@app.post("/chat")
def chat() -> tuple:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        reply = "Sorry, I glitched. Try again."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line("/chat", request_id, "", "", "", "", "400", elapsed_ms)
        return jsonify({"ok": False, "reply": reply, "error": "invalid_json"}), 400

    client_req_id = (data.get("client_req_id") or "").strip()
    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    object_key = (data.get("object_key") or "").strip()
    region = (data.get("region") or "").strip()
    timestamp = (data.get("ts") or datetime.now(timezone.utc).isoformat())

    if not message:
        reply = "Sorry, I glitched. Try again."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "400",
            elapsed_ms,
        )
        return jsonify({"ok": False, "reply": reply, "error": "message_required"}), 400

    if not OPENAI_API_KEY:
        reply = "Server misconfigured (missing OPENAI_API_KEY)."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "500",
            elapsed_ms,
        )
        log_error(f"request_id={request_id} configuration error: OPENAI_API_KEY missing")
        return (
            jsonify(
                {
                    "ok": False,
                    "reply": reply,
                    "error": "OPENAI_API_KEY missing",
                }
            ),
            500,
        )

    try:
        history = load_history(avatar_key, last_n=8)
        messages = build_messages(history, message)
        instructions = build_instructions(npc_id)

        reply_text = ""
        error_message = ""
        had_exception = False

        try:
            if hasattr(CLIENT, "responses"):
                response = CLIENT.responses.create(
                    model=OPENAI_MODEL,
                    instructions=instructions,
                    input=messages,
                )
                reply_text = (response.output_text or "").strip()
            else:
                log_error(
                    "ERROR: OpenAI SDK outdated; missing .responses. "
                    "Upgrade via `python -m pip install -U openai`. "
                    f"request_id={request_id}"
                )
                resp = CLIENT.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "system", "content": instructions}] + messages,
                )
                reply_text = (resp.choices[0].message.content or "").strip()
            if not reply_text:
                error_message = "empty_reply"
        except Exception as exc:
            error_message = safe_error_reason(exc)
            had_exception = True
            log_openai_exception(request_id, exc)

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
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            str(status_code),
            elapsed_ms,
        )

        response_payload: dict[str, Any] = {
            "ok": ok,
            "reply": reply_text,
            "reply_chars": len(reply_text),
        }
        if not ok:
            response_payload["error"] = error_message
            if had_exception:
                response_payload["request_id"] = request_id

        return jsonify(response_payload), status_code
    except Exception as exc:
        log_unhandled_exception(request_id, exc)
        reply = "Sorry, I glitched. Try again."
        error_message = safe_error_reason(exc)
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "502",
            elapsed_ms,
        )
        return (
            jsonify(
                {
                    "ok": False,
                    "reply": reply,
                    "error": error_message,
                    "request_id": request_id,
                }
            ),
            502,
        )


if __name__ == "__main__":
    from waitress import serve

    ensure_dir(LOGS_ROOT)
    ensure_dir(CHAT_ROOT)
    serve(app, host="0.0.0.0", port=PORT)
