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
from flask import Flask, Response, request
import openai as openai_pkg
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "SLQuest.env")

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8001"))
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5.2").strip() or "gpt-5.2"
WEB_SEARCH_ENABLED = (os.getenv("WEB_SEARCH_ENABLED") or "0").strip() == "1"
WEB_SEARCH_ALLOWED_DOMAINS = (os.getenv("WEB_SEARCH_ALLOWED_DOMAINS") or "").strip()

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


def is_conversation_invalid(exc: Exception) -> bool:
    message = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True
    markers = ["conversation", "not found", "invalid", "no such", "missing"]
    return any(marker in message for marker in markers)


log_line(RUN_LOG_PATH, f"OpenAI SDK version: {openai_pkg.__version__}")

if not OPENAI_API_KEY:
    ensure_dir(LOGS_ROOT)
    startup_message = "ERROR: OPENAI_API_KEY missing. Update SLQuest.env and restart."
    log_line(RUN_LOG_PATH, startup_message)
    print(startup_message)


def sanitize_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")
    return cleaned or "unknown"


def thread_key(avatar_key: str, npc_id: str) -> str:
    return f"{avatar_key}__{npc_id}"


def thread_dir(avatar_key: str, npc_id: str) -> Path:
    safe_avatar = sanitize_key(avatar_key)
    safe_npc = sanitize_key(npc_id)
    directory = CHAT_ROOT / safe_avatar / "threads" / safe_npc
    ensure_dir(directory)
    return directory


def conversation_id_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "conversation_id.txt"


def history_jsonl_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "history.jsonl"


def thread_metadata_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "thread.json"


def load_history(avatar_key: str, npc_id: str, last_n: int) -> list[dict[str, Any]]:
    path = history_jsonl_path(avatar_key, npc_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return events[-last_n:]


def append_history(avatar_key: str, npc_id: str, event: dict[str, Any]) -> None:
    path = history_jsonl_path(avatar_key, npc_id)
    line = json.dumps(event, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_conversation_id(avatar_key: str, npc_id: str) -> str | None:
    path = conversation_id_path(avatar_key, npc_id)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def save_conversation_id(avatar_key: str, npc_id: str, conversation_id: str) -> None:
    path = conversation_id_path(avatar_key, npc_id)
    path.write_text(conversation_id.strip(), encoding="utf-8")


def delete_conversation_id(avatar_key: str, npc_id: str) -> None:
    path = conversation_id_path(avatar_key, npc_id)
    if path.exists():
        path.unlink()


def update_thread_metadata(avatar_key: str, npc_id: str, metadata: dict[str, Any]) -> None:
    path = thread_metadata_path(avatar_key, npc_id)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}
    existing.update(metadata)
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


def sanitize_punctuation(text: str) -> str:
    replacements = {
        "\u2019": "'",
        "\u2018": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
    }
    return text.translate(str.maketrans(replacements))


def json_response(payload: dict[str, Any], status_code: int) -> tuple[Response, int]:
    return (
        Response(
            json.dumps(payload, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
        ),
        status_code,
    )


def build_instructions(npc_id: str) -> str:
    return (
        "You are an SLQuest NPC chatting in Second Life. "
        f"NPC ID: {npc_id}. "
        "Reply must be short for Second Life: aim <= 900 characters. "
        "No markdown. One message only. "
        "Use web search only if the user asks for up-to-date facts or checking something online; "
        "otherwise answer from conversation context."
    )


def build_messages(history: list[dict[str, Any]], message: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for event in history:
        direction = event.get("direction")
        content = event.get("text")
        if direction == "in":
            role = "user"
        elif direction == "out":
            role = "assistant"
        else:
            role = None
        if role and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


def parse_allowed_domains(raw_domains: str) -> list[str]:
    if not raw_domains:
        return []
    domains = [domain.strip() for domain in raw_domains.split(",")]
    return [domain for domain in domains if domain]


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def log_web_search_state(
    request_id: str, client_req_id: str, enabled: bool, allowed_domains: list[str]
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    domains = ",".join(allowed_domains) if allowed_domains else "-"
    line = (
        f"[{timestamp}] request_id={request_id} client_req_id={client_req_id or '-'} "
        f"web_search_enabled={int(enabled)} allowed_domains={domains}"
    )
    log_line(RUN_LOG_PATH, line)


def extract_web_search_sources(response: Any) -> list[str]:
    sources: list[str] = []
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return sources
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type != "web_search_call":
            continue
        action = getattr(item, "action", None)
        action_sources = getattr(action, "sources", None) if action else None
        if not isinstance(action_sources, list):
            continue
        for source in action_sources:
            url = None
            if isinstance(source, dict):
                url = source.get("url") or source.get("source") or source.get("title")
            else:
                url = getattr(source, "url", None) or getattr(source, "source", None)
            if isinstance(url, str) and url:
                sources.append(url)
            elif isinstance(source, str) and source:
                sources.append(source)
    seen: set[str] = set()
    unique_sources = []
    for source in sources:
        if source not in seen:
            seen.add(source)
            unique_sources.append(source)
    return unique_sources


def log_web_search_sources(
    request_id: str, client_req_id: str, sources: list[str], limit: int = 5
) -> None:
    if not sources:
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trimmed = sources[:limit]
    source_list = ", ".join(trimmed)
    line = (
        f"[{timestamp}] request_id={request_id} client_req_id={client_req_id or '-'} "
        f"web_search_sources={source_list}"
    )
    log_line(RUN_LOG_PATH, line)


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
        return json_response({"ok": False, "reply": reply, "error": "invalid_json"}, 400)

    client_req_id = (data.get("client_req_id") or "").strip()
    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    object_key = (data.get("object_key") or "").strip()
    region = (data.get("region") or "").strip()
    timestamp = (data.get("ts") or datetime.now(timezone.utc).isoformat())
    allow_web_search = parse_bool(data.get("allow_web_search"))

    allowed_domains = parse_allowed_domains(WEB_SEARCH_ALLOWED_DOMAINS)
    effective_web = WEB_SEARCH_ENABLED and allow_web_search
    log_web_search_state(request_id, client_req_id, effective_web, allowed_domains)

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
        return json_response(
            {"ok": False, "reply": reply, "error": "message_required"}, 400
        )

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
        return json_response(
            {
                "ok": False,
                "reply": reply,
                "error": "OPENAI_API_KEY missing",
            },
            500,
        )

    try:
        instructions = build_instructions(npc_id)
        thread_key_value = thread_key(avatar_key, npc_id)
        update_thread_metadata(
            avatar_key,
            npc_id,
            {
                "thread_key": thread_key_value,
                "avatar_uuid": avatar_key,
                "npc_id": npc_id,
                "last_seen": timestamp,
            },
        )

        reply_text = ""
        error_message = ""
        had_exception = False
        use_conversation = hasattr(CLIENT, "conversations")
        conversation_id = None
        conversation_failure = None

        if use_conversation:
            try:
                conversation_id = load_conversation_id(avatar_key, npc_id)
                if not conversation_id:
                    conversation = CLIENT.conversations.create()
                    conversation_id = getattr(conversation, "id", None)
                    if conversation_id:
                        save_conversation_id(avatar_key, npc_id, conversation_id)
                    else:
                        raise RuntimeError("conversation_create_empty_id")
            except Exception as exc:
                conversation_id = None
                conversation_failure = exc
                log_openai_exception(request_id, exc)

        def request_openai(use_thread: bool) -> str:
            if hasattr(CLIENT, "responses"):
                request_payload: dict[str, Any] = {
                    "model": OPENAI_MODEL,
                    "instructions": instructions,
                    "tool_choice": "auto",
                }
                if use_thread and conversation_id:
                    request_payload["input"] = message
                    request_payload["conversation"] = conversation_id
                    request_payload["truncation"] = "auto"
                else:
                    history = load_history(avatar_key, npc_id, last_n=8)
                    request_payload["input"] = build_messages(history, message)
                if effective_web:
                    if allowed_domains:
                        request_payload["tools"] = [
                            {
                                "type": "web_search",
                                "filters": {"allowed_domains": allowed_domains},
                            }
                        ]
                    else:
                        request_payload["tools"] = [{"type": "web_search"}]
                    request_payload["include"] = ["web_search_call.action.sources"]
                response = CLIENT.responses.create(**request_payload)
                if effective_web:
                    sources = extract_web_search_sources(response)
                    log_web_search_sources(request_id, client_req_id, sources)
                return (response.output_text or "").strip()
            log_error(
                "ERROR: OpenAI SDK outdated; missing .responses. "
                "Upgrade via `python -m pip install -U openai`. "
                f"request_id={request_id}"
            )
            history = load_history(avatar_key, npc_id, last_n=8)
            messages = build_messages(history, message)
            resp = CLIENT.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "system", "content": instructions}] + messages,
            )
            return (resp.choices[0].message.content or "").strip()

        try:
            if conversation_id and not conversation_failure:
                try:
                    reply_text = request_openai(use_thread=True)
                except Exception as exc:
                    if is_conversation_invalid(exc):
                        delete_conversation_id(avatar_key, npc_id)
                        conversation_id = None
                        reply_text = request_openai(use_thread=False)
                    else:
                        raise
            else:
                reply_text = request_openai(use_thread=False)
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

        reply_text = clamp_reply(sanitize_punctuation(reply_text))

        user_event = {
            "ts": timestamp,
            "direction": "in",
            "avatar_uuid": avatar_key,
            "npc_id": npc_id,
            "thread_key": thread_key_value,
            "text": message,
            "object_key": object_key,
            "region": region,
            "client_req_id": client_req_id,
            "request_id": request_id,
        }
        assistant_event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": "out",
            "avatar_uuid": avatar_key,
            "npc_id": npc_id,
            "thread_key": thread_key_value,
            "text": reply_text,
            "object_key": object_key,
            "region": region,
            "client_req_id": client_req_id,
            "request_id": request_id,
            "error": error_message or None,
        }
        append_history(avatar_key, npc_id, user_event)
        append_history(avatar_key, npc_id, assistant_event)

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

        return json_response(response_payload, status_code)
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
        return json_response(
            {
                "ok": False,
                "reply": reply,
                "error": error_message,
                "request_id": request_id,
            },
            502,
        )


if __name__ == "__main__":
    from waitress import serve

    ensure_dir(LOGS_ROOT)
    ensure_dir(CHAT_ROOT)
    serve(app, host="0.0.0.0", port=PORT)
