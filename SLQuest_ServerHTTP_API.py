from __future__ import annotations

import json
import os
import re
import time
import traceback
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
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
STATE_ROOT = BASE_DIR / "state"
NPCS_ROOT = BASE_DIR / "npcs"
NPC_BASE_DIR = NPCS_ROOT / "_base"
NPC_BASE_SYSTEM_PATH = NPC_BASE_DIR / "system.md"
NPC_GENERAL_SYSTEM_PATH = NPCS_ROOT / "general_npc.md"
OPENAI_TRACE_DIR = LOGS_ROOT / "openai_requests"
SLQUEST_ADMIN_TOKEN = (os.getenv("SLQUEST_ADMIN_TOKEN") or "").strip()
RUN_LOG_PATH = LOGS_ROOT / f"SLQuest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
ERROR_LOG_PATH = LOGS_ROOT / "SLQuest_errors.log"
PROFILE_CARD_TTL_DAYS = int(os.getenv("PROFILE_CARD_TTL_DAYS", "7"))
PROFILE_ENRICHER_URL = (os.getenv("PROFILE_ENRICHER_URL") or "http://localhost:8002/profile/enrich").strip()
PROFILE_ENRICHER_ENABLED = (os.getenv("PROFILE_ENRICHER_ENABLED") or "1").strip() == "1"
PROFILE_ENRICHER_TIMEOUT_SECONDS = float(os.getenv("PROFILE_ENRICHER_TIMEOUT_SECONDS", "0.6"))

CLIENT = OpenAI(api_key=OPENAI_API_KEY)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def log_line(path: Path, line: str) -> None:
    ensure_dir(LOGS_ROOT)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def valid_npc_id(npc_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", npc_id))


def npc_profile_dir(npc_id: str) -> Path:
    return NPCS_ROOT / npc_id


def npc_system_path(npc_id: str) -> Path:
    return npc_profile_dir(npc_id) / "system.md"


def npc_config_path(npc_id: str) -> Path:
    return npc_profile_dir(npc_id) / "config.json"


def load_npc_config(npc_id: str) -> dict[str, Any]:
    defaults = {"model": OPENAI_MODEL, "max_history_events": 8, "display_name": npc_id}
    path = npc_config_path(npc_id)
    if not path.exists():
        return defaults
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    if not isinstance(loaded, dict):
        return defaults
    merged = defaults.copy()
    merged.update(loaded)
    return merged


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


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_payload(item) for key, item in value.items()}
    return value


def log_openai_request(
    request_id: str,
    client_req_id: str,
    avatar_key: str,
    npc_id: str,
    payload: dict[str, Any],
) -> None:
    ensure_dir(OPENAI_TRACE_DIR)
    trace_path = OPENAI_TRACE_DIR / f"{request_id}_{int(time.time() * 1000)}.json"
    trace_payload = {
        "request_id": request_id,
        "client_req_id": client_req_id,
        "avatar_key": avatar_key,
        "npc_id": npc_id,
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload": redact_payload(payload),
    }
    atomic_write_json(trace_path, trace_payload)


def trim_log_text(text: str, max_len: int = 800) -> str:
    cleaned = text.replace("\n", " ").replace("\r", " ")
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "…"


def log_incoming_request(
    endpoint: str,
    request_id: str,
    client_req_id: str,
    avatar_key: str,
    npc_id: str,
    payload: Any,
    raw_body: str,
    note: str = "",
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_payload = redact_payload(payload)
    payload_text = trim_log_text(json.dumps(safe_payload, ensure_ascii=False))
    raw_text = trim_log_text(raw_body)
    line = (
        f"[{timestamp}] endpoint={endpoint} request_id={request_id} "
        f"client_req_id={client_req_id or '-'} avatar_key={avatar_key or '-'} "
        f"npc_id={npc_id or '-'} note={note or '-'} "
        f"content_type={request.content_type or '-'} "
        f"content_length={request.content_length or '-'} "
        f"payload={payload_text} raw_body=\"{raw_text}\""
    )
    log_line(RUN_LOG_PATH, line)


def log_response_payload(
    endpoint: str,
    request_id: str,
    client_req_id: str,
    avatar_key: str,
    npc_id: str,
    status_code: int,
    payload: dict[str, Any],
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_payload = redact_payload(payload)
    payload_text = trim_log_text(json.dumps(safe_payload, ensure_ascii=False))
    line = (
        f"[{timestamp}] endpoint={endpoint} request_id={request_id} "
        f"client_req_id={client_req_id or '-'} avatar_key={avatar_key or '-'} "
        f"npc_id={npc_id or '-'} status={status_code} response={payload_text}"
    )
    log_line(RUN_LOG_PATH, line)


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
ensure_dir(NPCS_ROOT)

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


def instructions_hash_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "instructions_hash.txt"


def history_json_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "history.json"


def thread_metadata_path(avatar_key: str, npc_id: str) -> Path:
    return thread_dir(avatar_key, npc_id) / "thread.json"


def state_avatar_dir(avatar_key: str) -> Path:
    return STATE_ROOT / sanitize_key(avatar_key)


def profile_card_path(avatar_key: str) -> Path:
    return state_avatar_dir(avatar_key) / "profile_card.json"


def load_profile_card(avatar_key: str) -> dict[str, Any] | None:
    path = profile_card_path(avatar_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def parse_profile_card_timestamp(card: dict[str, Any]) -> datetime | None:
    value = card.get("source_notes", {}).get("last_updated_utc")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_profile_card_fresh(card: dict[str, Any]) -> bool:
    last_updated = parse_profile_card_timestamp(card)
    if not last_updated:
        return False
    return datetime.now(timezone.utc) - last_updated < timedelta(days=PROFILE_CARD_TTL_DAYS)


def trigger_profile_enricher(avatar_key: str, force: bool = False) -> None:
    if not PROFILE_ENRICHER_ENABLED:
        return
    if not PROFILE_ENRICHER_URL:
        return
    payload = json.dumps({"avatar_uuid": avatar_key, "force": force}).encode("utf-8")
    request_obj = Request(
        PROFILE_ENRICHER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request_obj, timeout=PROFILE_ENRICHER_TIMEOUT_SECONDS) as response:
            response.read()
    except (URLError, HTTPError, TimeoutError) as exc:
        log_error(f"profile_enricher_failed avatar={avatar_key} error={exc}")


def ensure_profile_card(avatar_key: str) -> dict[str, Any] | None:
    if not avatar_key:
        return None
    card = load_profile_card(avatar_key)
    is_fresh = bool(card and is_profile_card_fresh(card))
    if not is_fresh:
        trigger_profile_enricher(avatar_key, force=False)
        card = load_profile_card(avatar_key) or card
    return card


def build_personalization_snippet(card: dict[str, Any] | None) -> str:
    if not card:
        return ""
    display_name = card.get("display_name") or ""
    keywords = card.get("profile_keywords") or []
    vibe_tags = card.get("image_vibe_tags") or []
    safe = card.get("safe_personalization") or {}
    topics = safe.get("topics_to_offer") or []
    tone_avoid = safe.get("tone_avoid") or []
    parts = [
        "Personalization context (use lightly; do not mention you looked it up):",
    ]
    if display_name:
        parts.append(f"- display_name: {display_name}")
    if keywords:
        parts.append(
            f\"- profile keywords: {', '.join(str(item) for item in keywords[:6])}\"
        )
    if vibe_tags:
        parts.append(
            f\"- vibe tags: {', '.join(str(item) for item in vibe_tags[:6])}\"
        )
    if topics:
        parts.append(
            f\"- topics to offer: {', '.join(str(item) for item in topics[:6])}\"
        )
    if tone_avoid:
        parts.append(
            f\"- tone rules: avoid {', '.join(str(item) for item in tone_avoid[:6])}\"
        )
    if len(parts) <= 1:
        return ""
    return "\n".join(parts)

def load_history(avatar_key: str, npc_id: str, last_n: int) -> list[dict[str, Any]]:
    if last_n <= 0:
        return []
    path = history_json_path(avatar_key, npc_id)
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(loaded, list):
        events = [entry for entry in loaded if isinstance(entry, dict)]
    else:
        events = []
    return events[-last_n:]


def append_history(avatar_key: str, npc_id: str, event: dict[str, Any]) -> None:
    path = history_json_path(avatar_key, npc_id)
    events: list[dict[str, Any]] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                events = [entry for entry in loaded if isinstance(entry, dict)]
        except json.JSONDecodeError:
            events = []
    events.append(event)
    atomic_write_json(path, events)


def load_conversation_id(avatar_key: str, npc_id: str) -> str | None:
    path = conversation_id_path(avatar_key, npc_id)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def load_instructions_hash(avatar_key: str, npc_id: str) -> str | None:
    path = instructions_hash_path(avatar_key, npc_id)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def save_conversation_id(avatar_key: str, npc_id: str, conversation_id: str) -> None:
    path = conversation_id_path(avatar_key, npc_id)
    path.write_text(conversation_id.strip(), encoding="utf-8")


def save_instructions_hash(avatar_key: str, npc_id: str, instructions_hash: str) -> None:
    path = instructions_hash_path(avatar_key, npc_id)
    path.write_text(instructions_hash.strip(), encoding="utf-8")


def delete_conversation_id(avatar_key: str, npc_id: str) -> None:
    path = conversation_id_path(avatar_key, npc_id)
    if path.exists():
        path.unlink()


def delete_instructions_hash(avatar_key: str, npc_id: str) -> None:
    path = instructions_hash_path(avatar_key, npc_id)
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


def build_instructions(npc_id: str, profile_card: dict[str, Any] | None = None) -> str:
    fallback = (
        "You are an SLQuest NPC chatting in Second Life. "
        f"NPC ID: {npc_id}. "
        "Reply must be short for Second Life: aim <= 900 characters. "
        "No markdown. One message only. "
        "Use web search only if the user asks for up-to-date facts or checking something online; "
        "otherwise answer from conversation context."
    )
    base_text = read_text_if_exists(NPC_BASE_SYSTEM_PATH).strip()
    general_text = read_text_if_exists(NPC_GENERAL_SYSTEM_PATH).strip()
    npc_text = read_text_if_exists(npc_system_path(npc_id)).strip()
    personalization = build_personalization_snippet(profile_card)
    if not base_text and not general_text and not npc_text:
        if personalization:
            return fallback + "\n\n" + personalization
        return fallback
    parts: list[str] = []
    if base_text:
        parts.append(base_text)
    if general_text:
        parts.append(general_text)
    parts.append(f"NPC ID: {npc_id}.")
    if npc_text:
        parts.append(npc_text)
    if personalization:
        parts.append(personalization)
    return "\n\n".join(parts)


def hash_instructions(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


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


def create_conversation_with_developer(
    request_id: str,
    client_req_id: str,
    avatar_key: str,
    npc_id: str,
    instructions: str,
) -> str:
    request_payload = {
        "items": [
            {
                "type": "message",
                "role": "developer",
                "content": instructions,
            }
        ]
    }
    log_openai_request(request_id, client_req_id, avatar_key, npc_id, request_payload)
    conversation = CLIENT.conversations.create(**request_payload)
    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        raise RuntimeError("conversation_create_empty_id")
    return conversation_id


@app.post("/admin/npc/upsert")
def admin_npc_upsert() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/admin/npc/upsert",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line(
            "/admin/npc/upsert", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    admin_token = (data.get("admin_token") or "").strip()
    npc_id = (data.get("npc_id") or "").strip()
    system_prompt = data.get("system_prompt") or ""

    log_incoming_request(
        "/admin/npc/upsert",
        request_id,
        "",
        "",
        npc_id,
        data,
        raw_body,
        note="received",
    )

    if admin_token != SLQUEST_ADMIN_TOKEN:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/npc/upsert", request_id, "", "", npc_id, "", "403", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "forbidden", "request_id": request_id}, 403
        )

    if not valid_npc_id(npc_id):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/npc/upsert", request_id, "", "", npc_id, "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_npc_id", "request_id": request_id}, 400
        )

    if not isinstance(system_prompt, str):
        system_prompt = str(system_prompt)

    display_name = (data.get("display_name") or "").strip()
    model = (data.get("model") or "").strip()
    max_history_raw = data.get("max_history_events")

    profile_dir = npc_profile_dir(npc_id)
    ensure_dir(profile_dir)
    atomic_write_text(npc_system_path(npc_id), system_prompt)

    existing_config = load_npc_config(npc_id)
    new_config = dict(existing_config)
    if display_name:
        new_config["display_name"] = display_name
    if model:
        new_config["model"] = model
    if max_history_raw is not None:
        try:
            new_config["max_history_events"] = int(max_history_raw)
        except (TypeError, ValueError):
            pass
    atomic_write_json(npc_config_path(npc_id), new_config)

    index_path = NPCS_ROOT / "index.json"
    registry: dict[str, Any] = {"npcs": {}}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                registry = loaded
        except json.JSONDecodeError:
            registry = {"npcs": {}}
    if not isinstance(registry.get("npcs"), dict):
        registry["npcs"] = {}
    registry["npcs"][npc_id] = {
        "display_name": new_config.get("display_name", npc_id),
        "path": npc_id,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    atomic_write_json(index_path, registry)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/admin/npc/upsert",
        request_id,
        "",
        "",
        npc_id,
        system_prompt,
        "200",
        elapsed_ms,
    )
    response_payload = {"ok": True, "npc_id": npc_id, "updated": True}
    log_response_payload(
        "/admin/npc/upsert", request_id, "", "", npc_id, 200, response_payload
    )
    return json_response(response_payload, 200)


@app.post("/admin/conversation/reset")
def admin_conversation_reset() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/admin/conversation/reset",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line(
            "/admin/conversation/reset", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    admin_token = (data.get("admin_token") or "").strip()
    avatar_uuid = (data.get("avatar_uuid") or "").strip()
    npc_id = (data.get("npc_id") or "").strip()

    log_incoming_request(
        "/admin/conversation/reset",
        request_id,
        "",
        avatar_uuid,
        npc_id,
        data,
        raw_body,
        note="received",
    )

    if SLQUEST_ADMIN_TOKEN and admin_token != SLQUEST_ADMIN_TOKEN:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/conversation/reset",
            request_id,
            "",
            avatar_uuid,
            npc_id,
            "",
            "403",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "forbidden", "request_id": request_id}, 403
        )

    if not avatar_uuid or not npc_id:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/conversation/reset",
            request_id,
            "",
            avatar_uuid,
            npc_id,
            "",
            "400",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "missing_fields", "request_id": request_id}, 400
        )

    delete_conversation_id(avatar_uuid, npc_id)
    delete_instructions_hash(avatar_uuid, npc_id)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/admin/conversation/reset",
        request_id,
        "",
        avatar_uuid,
        npc_id,
        "",
        "200",
        elapsed_ms,
    )
    response_payload = {
        "ok": True,
        "avatar_uuid": avatar_uuid,
        "npc_id": npc_id,
        "reset": True,
    }
    log_response_payload(
        "/admin/conversation/reset",
        request_id,
        "",
        avatar_uuid,
        npc_id,
        200,
        response_payload,
    )
    return json_response(response_payload, 200)


@app.post("/admin/profile/refresh")
def admin_profile_refresh() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/admin/profile/refresh",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line(
            "/admin/profile/refresh", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    admin_token = (data.get("admin_token") or "").strip()
    avatar_uuid = (data.get("avatar_uuid") or "").strip()

    log_incoming_request(
        "/admin/profile/refresh",
        request_id,
        "",
        avatar_uuid,
        "",
        data,
        raw_body,
        note="received",
    )

    if SLQUEST_ADMIN_TOKEN and admin_token != SLQUEST_ADMIN_TOKEN:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/profile/refresh",
            request_id,
            "",
            avatar_uuid,
            "",
            "",
            "403",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "forbidden", "request_id": request_id}, 403
        )

    if not avatar_uuid:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/admin/profile/refresh",
            request_id,
            "",
            avatar_uuid,
            "",
            "",
            "400",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "missing_avatar_uuid", "request_id": request_id},
            400,
        )

    trigger_profile_enricher(avatar_uuid, force=True)
    card = load_profile_card(avatar_uuid)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/admin/profile/refresh",
        request_id,
        "",
        avatar_uuid,
        "",
        "",
        "200",
        elapsed_ms,
    )
    response_payload = {"ok": True, "avatar_uuid": avatar_uuid, "refreshed": True}
    if card:
        response_payload["profile_card"] = card
    log_response_payload(
        "/admin/profile/refresh",
        request_id,
        "",
        avatar_uuid,
        "",
        200,
        response_payload,
    )
    return json_response(response_payload, 200)


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
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        reply = f"Error: invalid_json payload (request_id={request_id})."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/chat", request_id, "", "", "", data, raw_body, note="invalid_json"
        )
        log_request_line("/chat", request_id, "", "", "", "", "400", elapsed_ms)
        response_payload = {"ok": False, "reply": reply, "error": "invalid_json"}
        log_response_payload("/chat", request_id, "", "", "", 400, response_payload)
        return json_response(response_payload, 400)

    client_req_id = (data.get("client_req_id") or "").strip()
    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    object_key = (data.get("object_key") or "").strip()
    region = (data.get("region") or "").strip()
    timestamp = (data.get("ts") or datetime.now(timezone.utc).isoformat())
    allow_web_search = parse_bool(data.get("allow_web_search"))
    reset_conversation = parse_bool(data.get("reset_conversation"))

    log_incoming_request(
        "/chat",
        request_id,
        client_req_id,
        avatar_key,
        npc_id,
        data,
        raw_body,
        note="received",
    )

    allowed_domains = parse_allowed_domains(WEB_SEARCH_ALLOWED_DOMAINS)
    effective_web = WEB_SEARCH_ENABLED and allow_web_search
    log_web_search_state(request_id, client_req_id, effective_web, allowed_domains)

    if not message:
        reply = f"Error: message_required (request_id={request_id})."
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
        response_payload = {"ok": False, "reply": reply, "error": "message_required"}
        log_response_payload(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            400,
            response_payload,
        )
        return json_response(response_payload, 400)

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
        response_payload = {
            "ok": False,
            "reply": reply,
            "error": "OPENAI_API_KEY missing",
        }
        log_response_payload(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            500,
            response_payload,
        )
        return json_response(response_payload, 500)

    try:
        config = load_npc_config(npc_id)
        model = config.get("model", OPENAI_MODEL)
        try:
            max_history = int(config.get("max_history_events", 8))
        except (TypeError, ValueError):
            max_history = 8
        max_history = max(0, min(50, max_history))
        profile_card = None
        try:
            profile_card = ensure_profile_card(avatar_key)
        except Exception as exc:
            log_error(f"profile_card_load_failed avatar={avatar_key} error={exc}")
        instructions = build_instructions(npc_id, profile_card)
        instructions_hash = hash_instructions(instructions)
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
                if reset_conversation:
                    delete_conversation_id(avatar_key, npc_id)
                    delete_instructions_hash(avatar_key, npc_id)
                conversation_id = load_conversation_id(avatar_key, npc_id)
                stored_hash = load_instructions_hash(avatar_key, npc_id)
                if stored_hash != instructions_hash:
                    delete_conversation_id(avatar_key, npc_id)
                    delete_instructions_hash(avatar_key, npc_id)
                    conversation_id = None
                if not conversation_id:
                    # Developer prompt is stored once as a conversation item; we don’t resend instructions each turn.
                    conversation_id = create_conversation_with_developer(
                        request_id,
                        client_req_id,
                        avatar_key,
                        npc_id,
                        instructions,
                    )
                    save_conversation_id(avatar_key, npc_id, conversation_id)
                    save_instructions_hash(avatar_key, npc_id, instructions_hash)
            except Exception as exc:
                conversation_id = None
                conversation_failure = exc
                log_openai_exception(request_id, exc)

        def request_openai(use_thread: bool) -> str:
            if hasattr(CLIENT, "responses"):
                request_payload: dict[str, Any] = {
                    "model": model,
                    "tool_choice": "auto",
                }
                if use_thread and conversation_id:
                    request_payload["input"] = message
                    request_payload["conversation"] = conversation_id
                    request_payload["truncation"] = "auto"
                else:
                    request_payload["instructions"] = instructions
                    history = load_history(avatar_key, npc_id, last_n=max_history)
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
                log_openai_request(
                    request_id, client_req_id, avatar_key, npc_id, request_payload
                )
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
            history = load_history(avatar_key, npc_id, last_n=max_history)
            messages = build_messages(history, message)
            log_openai_request(
                request_id,
                client_req_id,
                avatar_key,
                npc_id,
                {
                    "model": model,
                    "messages": [{"role": "system", "content": instructions}]
                    + messages,
                },
            )
            resp = CLIENT.chat.completions.create(
                model=model,
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
                        try:
                            conversation_id = create_conversation_with_developer(
                                request_id,
                                client_req_id,
                                avatar_key,
                                npc_id,
                                instructions,
                            )
                            save_conversation_id(avatar_key, npc_id, conversation_id)
                            save_instructions_hash(
                                avatar_key, npc_id, instructions_hash
                            )
                            reply_text = request_openai(use_thread=True)
                        except Exception:
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
            reply_text = f"Error: upstream_reply_failed ({error_message or 'unknown'})."
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

        log_response_payload(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            status_code,
            response_payload,
        )
        return json_response(response_payload, status_code)
    except Exception as exc:
        log_unhandled_exception(request_id, exc)
        error_message = safe_error_reason(exc)
        reply = f"Error: server_exception ({error_message})."
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
        response_payload = {
            "ok": False,
            "reply": reply,
            "error": error_message,
            "request_id": request_id,
        }
        log_response_payload(
            "/chat",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            502,
            response_payload,
        )
        return json_response(response_payload, 502)


if __name__ == "__main__":
    from waitress import serve

    ensure_dir(LOGS_ROOT)
    ensure_dir(CHAT_ROOT)
    serve(app, host="0.0.0.0", port=PORT)
