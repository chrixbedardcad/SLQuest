from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, Response, request, has_request_context
import openai as openai_pkg
from openai import OpenAI

import SLQuest_QuestEngine as QuestEngine

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "SLQuest.env")

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8001"))
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5.2").strip() or "gpt-5.2"
WEB_SEARCH_ENABLED = (os.getenv("WEB_SEARCH_ENABLED") or "0").strip() == "1"
WEB_SEARCH_ALLOWED_DOMAINS = (os.getenv("WEB_SEARCH_ALLOWED_DOMAINS") or "").strip()
OPENAI_API_BASE = (os.getenv("OPENAI_API_BASE") or "https://api.openai.com").strip()

LOGS_ROOT = BASE_DIR / "logs"
CHAT_ROOT = BASE_DIR / "chat"
PROFILES_ROOT = BASE_DIR / "profiles"
NPCS_ROOT = BASE_DIR / "npcs"
POOL_DIR = BASE_DIR / "pools"
POOL_FILE = POOL_DIR / "objects.json"
PLAYER_STATE_DIR = BASE_DIR / "quests" / "player"
NPC_BASE_DIR = NPCS_ROOT / "_base"
NPC_BASE_SYSTEM_PATH = NPC_BASE_DIR / "system.md"
NPC_BASE_FIRST_CONVERSATION_PATH = NPC_BASE_DIR / "first_conversation.md"
NPC_GENERAL_SYSTEM_PATH = NPCS_ROOT / "general_npc.md"
OPENAI_TRACE_DIR = LOGS_ROOT / "openai_requests"
SLQUEST_ADMIN_TOKEN = (os.getenv("SLQUEST_ADMIN_TOKEN") or "").strip()
RUN_LOG_PATH = LOGS_ROOT / f"SLQuest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
ERROR_LOG_PATH = LOGS_ROOT / "SLQuest_errors.log"
PROFILE_CARD_TTL_DAYS = int(os.getenv("PROFILE_CARD_TTL_DAYS", "7"))
PROFILE_ENRICHER_URL = (os.getenv("PROFILE_ENRICHER_URL") or "http://localhost:8002/profile/enrich").strip()
PROFILE_ENRICHER_ENABLED = (os.getenv("PROFILE_ENRICHER_ENABLED") or "1").strip() == "1"
PROFILE_ENRICHER_TIMEOUT_SECONDS = float(os.getenv("PROFILE_ENRICHER_TIMEOUT_SECONDS", "0.6"))
CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS = float(
    os.getenv("CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS", "0.6")
)
CALLBACKS_LOCK = threading.Lock()
CALLBACKS: dict[tuple[str, str], dict[str, Any]] = {}
CALLBACK_TTL_SECONDS = int(os.getenv("CALLBACK_TTL_SECONDS", "7200"))
CALLBACKS_FILE = os.path.join(os.path.dirname(__file__), "data", "callbacks.json")
CALLBACK_POST_TIMEOUT_SECONDS = float(os.getenv("CALLBACK_POST_TIMEOUT_SECONDS", "2.5"))
SAFE_CALLBACK_MAX = 1400
PKG_CACHE_LOCK = threading.Lock()
PKG_CACHE: dict[str, dict[str, Any]] = {}
PKG_CACHE_TTL_SECONDS = 90

CLIENT = OpenAI(api_key=OPENAI_API_KEY)


def esc(value: str) -> str:
    return quote(value or "", safe="")


def kv(key: str, val: str) -> str:
    return f"{key}={esc(val)}"


def pack(fields: list[tuple[str, str]]) -> str:
    return "|".join(
        [f"{key}={esc(val)}" for (key, val) in fields if key and val is not None]
    )


def pack_actions(actions: list[str]) -> str:
    return ";".join(
        [action.strip() for action in actions if isinstance(action, str) and action.strip()]
    )


def pack_quest(q: dict[str, Any]) -> str:
    parts = []
    for key in ("quest_id", "state", "hint", "reward"):
        value = q.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    return ";".join(parts)


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


def npc_first_conversation_path(npc_id: str) -> Path:
    return npc_profile_dir(npc_id) / "first_conversation.md"


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


def log_startup_status() -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_line(
        RUN_LOG_PATH,
        f"[{timestamp}] server_starting port={PORT} openai_model={OPENAI_MODEL} "
        f"openai_base={OPENAI_API_BASE} web_search_enabled={int(WEB_SEARCH_ENABLED)} "
        f"profile_enricher_enabled={int(PROFILE_ENRICHER_ENABLED)}",
    )
    log_line(
        RUN_LOG_PATH,
        f"[{timestamp}] server_paths logs_root={LOGS_ROOT} chat_root={CHAT_ROOT} "
        f"profiles_root={PROFILES_ROOT} npcs_root={NPCS_ROOT}",
    )
    log_line(
        RUN_LOG_PATH,
        f"[{timestamp}] server_config admin_token_set={int(bool(SLQUEST_ADMIN_TOKEN))} "
        f"openai_key_set={int(bool(OPENAI_API_KEY))} "
        f"profile_enricher_url={PROFILE_ENRICHER_URL or '-'} "
        f"profile_enricher_timeout={PROFILE_ENRICHER_TIMEOUT_SECONDS}",
    )
    log_line(
        RUN_LOG_PATH,
        f"[{timestamp}] server_timeouts profile_enricher={PROFILE_ENRICHER_TIMEOUT_SECONDS} "
        f"conversation_add_item={CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS} "
        f"callback_post={CALLBACK_POST_TIMEOUT_SECONDS}",
    )


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
    if has_request_context():
        content_type = request.content_type or "-"
        content_length = request.content_length or "-"
    else:
        content_type = "-"
        content_length = "-"
    line = (
        f"[{timestamp}] endpoint={endpoint} request_id={request_id} "
        f"client_req_id={client_req_id or '-'} avatar_key={avatar_key or '-'} "
        f"npc_id={npc_id or '-'} note={note or '-'} "
        f"content_type={content_type} "
        f"content_length={content_length} "
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


def redact_callback_url(url: str) -> str:
    return re.sub(r"([?&]t=)[^&]+", r"\1***", url or "")


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


def now_utc_ts() -> float:
    return time.time()


def prune_callbacks() -> None:
    cutoff = now_utc_ts() - CALLBACK_TTL_SECONDS
    with CALLBACKS_LOCK:
        stale_keys = [
            key
            for key, entry in CALLBACKS.items()
            if entry.get("updated_at", 0) < cutoff
        ]
        for key in stale_keys:
            CALLBACKS.pop(key, None)


def make_callback_token() -> str:
    return uuid4().hex


def pkg_cache_put(body: str) -> str:
    token = uuid4().hex
    with PKG_CACHE_LOCK:
        PKG_CACHE[token] = {
            "body": body,
            "expires_at": now_utc_ts() + PKG_CACHE_TTL_SECONDS,
        }
    return token


def pkg_cache_get(token: str) -> str | None:
    now = now_utc_ts()
    with PKG_CACHE_LOCK:
        entry = PKG_CACHE.get(token)
        if not entry:
            return None
        if entry.get("expires_at", 0) < now:
            PKG_CACHE.pop(token, None)
            return None
        return entry.get("body")


def pkg_cache_prune() -> None:
    now = now_utc_ts()
    with PKG_CACHE_LOCK:
        stale = [key for key, value in PKG_CACHE.items() if value.get("expires_at", 0) < now]
        for key in stale:
            PKG_CACHE.pop(key, None)


POOL_LOCK = threading.Lock()
POOL_STALE_SECONDS = 600  # 10 minutes


def load_pool() -> dict[str, Any]:
    """Load the shared object pool."""
    if not POOL_FILE.exists():
        return {"objects": {}}
    try:
        loaded = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"objects": {}}
    if not isinstance(loaded, dict):
        return {"objects": {}}
    if not isinstance(loaded.get("objects"), dict):
        loaded["objects"] = {}
    return loaded


def save_pool(pool: dict[str, Any]) -> None:
    """Atomically save the pool."""
    ensure_dir(POOL_DIR)
    atomic_write_json(POOL_FILE, pool)


def get_active_objects(
    min_difficulty: int | None = None,
    max_difficulty: int | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Get active objects, filtering out stale (>10 min since last_seen)."""
    pool = load_pool()
    objects = pool.get("objects", {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=POOL_STALE_SECONDS)
    active = []
    for obj_id, obj_data in objects.items():
        if not isinstance(obj_data, dict):
            continue
        last_seen_str = obj_data.get("last_seen")
        if last_seen_str:
            try:
                last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen < cutoff:
                    continue  # Stale
            except (ValueError, TypeError):
                continue  # Invalid timestamp
        else:
            continue  # No timestamp
        # Apply filters
        difficulty = obj_data.get("difficulty", 1)
        if min_difficulty is not None and difficulty < min_difficulty:
            continue
        if max_difficulty is not None and difficulty > max_difficulty:
            continue
        if category is not None and obj_data.get("category") != category:
            continue
        active.append(obj_data)
    return active


def set_callback(object_key: str, npc_id: str, url: str, token: str, region: str) -> None:
    with CALLBACKS_LOCK:
        CALLBACKS[(object_key, npc_id)] = {
            "url": url,
            "token": token,
            "region": region,
            "updated_at": now_utc_ts(),
        }
    save_callbacks()


def save_callbacks() -> None:
    """Save callbacks to disk for persistence across restarts."""
    try:
        os.makedirs(os.path.dirname(CALLBACKS_FILE), exist_ok=True)
        with CALLBACKS_LOCK:
            data = {
                f"{k[0]}|{k[1]}": v
                for k, v in CALLBACKS.items()
            }
        with open(CALLBACKS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log_error(f"save_callbacks failed: {e}")


def load_callbacks() -> None:
    """Load callbacks from disk on startup."""
    global CALLBACKS
    if not os.path.exists(CALLBACKS_FILE):
        return
    try:
        with open(CALLBACKS_FILE, "r") as f:
            data = json.load(f)
        with CALLBACKS_LOCK:
            for key_str, entry in data.items():
                parts = key_str.split("|", 1)
                if len(parts) == 2:
                    CALLBACKS[(parts[0], parts[1])] = entry
        log_line(RUN_LOG_PATH, f"load_callbacks loaded={len(CALLBACKS)}")
    except Exception as e:
        log_error(f"load_callbacks failed: {e}")


def get_callback(object_key: str, npc_id: str) -> dict[str, Any] | None:
    prune_callbacks()
    with CALLBACKS_LOCK:
        entry = CALLBACKS.get((object_key, npc_id))
        return dict(entry) if entry else None


def post_callback(callback_url: str, token: str, body_text: str) -> tuple[bool, str]:
    sep = "&" if "?" in callback_url else "?"
    url = f"{callback_url}{sep}t={token}"
    request_obj = Request(
        url,
        data=body_text.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    try:
        start_time = time.perf_counter()
        with urlopen(request_obj, timeout=CALLBACK_POST_TIMEOUT_SECONDS) as response:
            response.read()
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", 200)
            log_line(
                RUN_LOG_PATH,
                "callback_post_response "
                f"url={redact_callback_url(url)} status={status_code} elapsed_ms={elapsed_ms}",
            )
            if 200 <= int(status_code) < 300:
                return True, ""
            return False, f"callback_status_{status_code}"
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)


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


def profile_avatar_dir(avatar_key: str) -> Path:
    return PROFILES_ROOT / sanitize_key(avatar_key)


def profile_card_path(avatar_key: str) -> Path:
    return profile_avatar_dir(avatar_key) / "profile_card.json"


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


def save_profile_card(avatar_key: str, card: dict[str, Any]) -> None:
    path = profile_card_path(avatar_key)
    atomic_write_json(path, card)


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


def normalize_username(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered.endswith(" resident"):
        return value[: -len(" resident")].strip()
    return value


def trigger_profile_enricher(
    avatar_key: str,
    force: bool = False,
    avatar_name: str = "",
    avatar_display_name: str = "",
    avatar_username: str = "",
) -> None:
    if not PROFILE_ENRICHER_ENABLED:
        log_line(RUN_LOG_PATH, f"profile_enricher_disabled avatar={avatar_key}")
        return
    if not PROFILE_ENRICHER_URL:
        log_error(f"profile_enricher_missing_url avatar={avatar_key}")
        return
    payload_dict = {"avatar_uuid": avatar_key, "force": force}
    if avatar_name:
        payload_dict["avatar_name"] = avatar_name
    if avatar_display_name:
        payload_dict["avatar_display_name"] = avatar_display_name
    if avatar_username:
        payload_dict["avatar_username"] = avatar_username
    payload = json.dumps(payload_dict).encode("utf-8")
    request_obj = Request(
        PROFILE_ENRICHER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        start_time = time.perf_counter()
        with urlopen(request_obj, timeout=PROFILE_ENRICHER_TIMEOUT_SECONDS) as response:
            response.read()
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", "unknown")
            log_line(
                RUN_LOG_PATH,
                f"profile_enricher_ok avatar={avatar_key} status={status_code} elapsed_ms={elapsed_ms}",
            )
    except (URLError, HTTPError, TimeoutError) as exc:
        log_error(f"profile_enricher_failed avatar={avatar_key} error={exc}")


def ensure_profile_card(
    avatar_key: str,
    avatar_name: str = "",
    avatar_display_name: str = "",
    avatar_username: str = "",
) -> dict[str, Any] | None:
    if not avatar_key:
        return None
    card = load_profile_card(avatar_key)
    is_fresh = bool(card and is_profile_card_fresh(card))
    if not is_fresh:
        log_line(RUN_LOG_PATH, f"profile_card_refresh_needed avatar={avatar_key}")
        trigger_profile_enricher(
            avatar_key,
            force=False,
            avatar_name=avatar_name,
            avatar_display_name=avatar_display_name,
            avatar_username=avatar_username,
        )
        card = load_profile_card(avatar_key) or card
    safe_display_name = avatar_display_name.strip()
    safe_username = normalize_username(avatar_username or avatar_name)
    if card and (safe_username or safe_display_name):
        updated = False
        username = (card.get("username") or "").strip()
        display_name = (card.get("display_name") or "").strip()
        if safe_username and (not username or username == "Unknown"):
            card["username"] = safe_username
            updated = True
        if safe_display_name and (not display_name or display_name == "Unknown"):
            card["display_name"] = safe_display_name
            updated = True
        if updated:
            source_notes = card.get("source_notes")
            if not isinstance(source_notes, dict):
                source_notes = {}
            if safe_username:
                source_notes["lsl_username_used"] = True
            if safe_display_name:
                source_notes["lsl_display_name_used"] = True
            source_notes["last_updated_utc"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            card["source_notes"] = source_notes
            save_profile_card(avatar_key, card)
    return card


def build_personalization_snippet(card: dict[str, Any] | None) -> str:
    if not card:
        return ""
    display_name = card.get("display_name") or ""
    username = card.get("username") or ""
    bio = card.get("bio") or {}
    about_summary = bio.get("about_summary") or ""
    visual = card.get("visual_profile") or {}
    style_tags = visual.get("style_tags") or card.get("image_vibe_tags") or []
    hair = visual.get("hair") or {}
    eyes = visual.get("eyes") or {}
    safe = card.get("safe_personalization") or {}
    hooks = safe.get("safe_hooks") or []
    parts = [
        "Personalization context (use lightly; do not mention you looked it up):",
    ]
    if display_name:
        parts.append(f"- display_name: {display_name}")
    if username and username != display_name:
        parts.append(f"- username: {username}")
    if about_summary:
        parts.append(f"- about: {about_summary[:240]}")
    if style_tags:
        parts.append(f"- style tags: {', '.join(str(item) for item in style_tags[:6])}")
    if hooks:
        parts.append(f"- safe hooks: {', '.join(str(item) for item in hooks[:2])}")
    hair_line = ", ".join(
        item
        for item in [hair.get("color"), hair.get("length"), hair.get("style")]
        if item
    )
    eyes_line = ", ".join(item for item in [eyes.get("color"), eyes.get("notes")] if item)
    if hair_line or eyes_line:
        parts.append(f"- hair/eyes: {hair_line or 'unspecified'}; {eyes_line or 'unspecified'}")
    if len(parts) <= 1:
        return ""
    # Keep this snippet short (<= ~12 lines) to avoid bloating prompts.
    return "\n".join(parts)


def profile_fingerprint(card: dict[str, Any] | None) -> str:
    if not card:
        return ""
    bio = card.get("bio") or {}
    about_summary = (bio.get("about_summary") or "").strip().lower()
    visual = card.get("visual_profile") or {}
    style_tags = visual.get("style_tags") or card.get("image_vibe_tags") or []
    safe = card.get("safe_personalization") or {}
    hooks = safe.get("safe_hooks") or []
    combined = "|".join(
        [
            about_summary,
            ",".join(str(item).strip().lower() for item in style_tags),
            ",".join(str(item).strip().lower() for item in hooks),
        ]
    )
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def add_conversation_developer_item(
    request_id: str, conversation_id: str, content: str
) -> bool:
    if not OPENAI_API_KEY:
        log_error(
            f"conversation_item_skipped request_id={request_id} reason=missing_api_key"
        )
        return False
    payload = {
        "items": [
            {
                "type": "message",
                "role": "developer",
                "content": content,
            }
        ]
    }
    url = f"{OPENAI_API_BASE}/v1/conversations/{conversation_id}/items"
    request_obj = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
        method="POST",
    )
    try:
        start_time = time.perf_counter()
        with urlopen(request_obj, timeout=CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS) as response:
            response.read()
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", "unknown")
            log_line(
                RUN_LOG_PATH,
                f"conversation_item_added request_id={request_id} conversation={conversation_id} "
                f"status={status_code} elapsed_ms={elapsed_ms} timeout={CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS}",
            )
            return True
    except (URLError, HTTPError, TimeoutError) as exc:
        log_error(
            f"conversation_item_failed request_id={request_id} conversation={conversation_id} "
            f"timeout={CONVERSATION_ADD_ITEM_TIMEOUT_SECONDS} error={exc}"
        )
        return False


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


def load_thread_metadata(avatar_key: str, npc_id: str) -> dict[str, Any]:
    path = thread_metadata_path(avatar_key, npc_id)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


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


def build_base_instructions(npc_id: str) -> str:
    base_text = read_text_if_exists(NPC_BASE_SYSTEM_PATH).strip()
    general_text = read_text_if_exists(NPC_GENERAL_SYSTEM_PATH).strip()
    npc_text = read_text_if_exists(npc_system_path(npc_id)).strip()
    if not base_text and not general_text and not npc_text:
        return (
            "You are an SLQuest NPC chatting in Second Life. "
            f"NPC ID: {npc_id}. "
            "Reply must be short for Second Life: aim <= 900 characters. "
            "No markdown. One message only. "
            "Use web search only if the user asks for up-to-date facts or checking something online; "
            "otherwise answer from conversation context."
        )
    parts: list[str] = []
    if base_text:
        parts.append(base_text)
    if general_text:
        parts.append(general_text)
    parts.append(f"NPC ID: {npc_id}.")
    if npc_text:
        parts.append(npc_text)
    return "\n\n".join(parts)


def build_first_conversation_prompt(
    npc_id: str, profile_card: dict[str, Any] | None, quest_context: str
) -> str:
    base_text = read_text_if_exists(NPC_BASE_FIRST_CONVERSATION_PATH).strip()
    npc_text = read_text_if_exists(npc_first_conversation_path(npc_id)).strip()
    parts: list[str] = []
    if base_text:
        parts.append(base_text)
    if npc_text:
        parts.append(npc_text)
    personalization = build_personalization_snippet(profile_card)
    if personalization:
        parts.append(personalization)
    if quest_context:
        parts.append(f"Current quest status:\n{quest_context}")
    if not parts:
        return ""
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


def run_chat_logic(
    endpoint: str,
    request_id: str,
    data: dict[str, Any],
    raw_body: str = "",
) -> tuple[dict[str, Any], int]:
    start_time = time.perf_counter()
    client_req_id = (data.get("client_req_id") or "").strip()
    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    avatar_name = (data.get("avatar_name") or "").strip()
    avatar_display_name = (data.get("avatar_display_name") or "").strip()
    avatar_username = (data.get("avatar_username") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    object_key = (data.get("object_key") or "").strip()
    region = (data.get("region") or "").strip()
    timestamp = (data.get("ts") or datetime.now(timezone.utc).isoformat())
    allow_web_search = parse_bool(data.get("allow_web_search"))
    reset_conversation = parse_bool(data.get("reset_conversation"))
    quest_context = (data.get("quest_context") or "").strip()
    llm_message = message
    if quest_context:
        llm_message = (
            "<<QUEST_CONTEXT>>\n"
            + quest_context
            + "\n<</QUEST_CONTEXT>>\nPlayer: "
            + message
        )

    log_incoming_request(
        endpoint,
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
            endpoint,
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
            endpoint,
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            400,
            response_payload,
        )
        return response_payload, 400

    if not OPENAI_API_KEY:
        reply = "Server misconfigured (missing OPENAI_API_KEY)."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            endpoint,
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
            endpoint,
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            500,
            response_payload,
        )
        return response_payload, 500

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
            profile_card = ensure_profile_card(
                avatar_key,
                avatar_name=avatar_name,
                avatar_display_name=avatar_display_name,
                avatar_username=avatar_username,
            )
        except Exception as exc:
            log_error(f"profile_card_load_failed avatar={avatar_key} error={exc}")
        instructions = build_instructions(npc_id, profile_card)
        base_instructions = build_base_instructions(npc_id)
        if profile_card:
            log_line(
                RUN_LOG_PATH,
                f"profile_card_applied avatar={avatar_key} npc_id={npc_id}",
            )
        instructions_hash = hash_instructions(base_instructions)
        thread_key_value = thread_key(avatar_key, npc_id)
        personalization_fingerprint = profile_fingerprint(profile_card)
        history_empty = len(load_history(avatar_key, npc_id, last_n=1)) == 0
        first_turn_prompt = (
            build_first_conversation_prompt(npc_id, profile_card, quest_context)
            if history_empty
            else ""
        )
        existing_metadata = load_thread_metadata(avatar_key, npc_id)
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
        conversation_created = False

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
                    conversation_created = True
                    log_line(
                        RUN_LOG_PATH,
                        f"conversation_created request_id={request_id} conversation={conversation_id} "
                        f"avatar={avatar_key or '-'} npc_id={npc_id or '-'}",
                    )
            except Exception as exc:
                conversation_id = None
                conversation_failure = exc
                log_openai_exception(request_id, exc)

        stored_fingerprint = existing_metadata.get("profile_fingerprint") or ""
        should_update_fingerprint = True
        if (
            conversation_id
            and personalization_fingerprint
            and personalization_fingerprint != stored_fingerprint
        ):
            if not conversation_created:
                update_text = build_personalization_snippet(profile_card)
                if update_text:
                    log_line(
                        RUN_LOG_PATH,
                        f"personalization_update_attempt request_id={request_id} "
                        f"conversation={conversation_id} avatar={avatar_key or '-'}",
                    )
                    added = add_conversation_developer_item(
                        request_id,
                        conversation_id,
                        f"Personalization update:\n{update_text}",
                    )
                    if not added:
                        should_update_fingerprint = False
                        log_error(
                            f"personalization_update_failed request_id={request_id} avatar={avatar_key}"
                        )
        if should_update_fingerprint:
            update_thread_metadata(
                avatar_key,
                npc_id,
                {"profile_fingerprint": personalization_fingerprint},
            )
        if conversation_created and first_turn_prompt:
            log_line(
                RUN_LOG_PATH,
                f"conversation_first_turn_prompt request_id={request_id} "
                f"conversation={conversation_id} avatar={avatar_key or '-'}",
            )
            added = add_conversation_developer_item(
                request_id,
                conversation_id,
                first_turn_prompt,
            )
            if not added:
                log_error(
                    f"conversation_first_turn_prompt_failed request_id={request_id} "
                    f"avatar={avatar_key}"
                )

        def request_openai(use_thread: bool) -> str:
            request_started = time.perf_counter()
            if hasattr(CLIENT, "responses"):
                request_payload: dict[str, Any] = {
                    "model": model,
                    "tool_choice": "auto",
                }
                if use_thread and conversation_id:
                    request_payload["input"] = llm_message
                    request_payload["conversation"] = conversation_id
                    request_payload["truncation"] = "auto"
                else:
                    instructions_for_request = instructions
                    if first_turn_prompt:
                        instructions_for_request = (
                            f"{instructions}\n\n{first_turn_prompt}"
                        )
                    request_payload["instructions"] = instructions_for_request
                    history = load_history(avatar_key, npc_id, last_n=max_history)
                    request_payload["input"] = build_messages(history, llm_message)
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
                log_line(
                    RUN_LOG_PATH,
                    f"openai_request_start request_id={request_id} mode={'thread' if use_thread else 'stateless'} "
                    f"conversation={conversation_id or '-'} model={model}",
                )
                response = CLIENT.responses.create(**request_payload)
                elapsed_ms = int((time.perf_counter() - request_started) * 1000)
                log_line(
                    RUN_LOG_PATH,
                    f"openai_request_done request_id={request_id} mode={'thread' if use_thread else 'stateless'} "
                    f"elapsed_ms={elapsed_ms} output_chars={len(response.output_text or '')}",
                )
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
            messages = build_messages(history, llm_message)
            log_openai_request(
                request_id,
                client_req_id,
                avatar_key,
                npc_id,
                {
                    "model": model,
                    "messages": [{"role": "system", "content": instructions}] + messages,
                },
            )
            log_line(
                RUN_LOG_PATH,
                f"openai_request_start request_id={request_id} mode=legacy model={model}",
            )
            resp = CLIENT.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": instructions}] + messages,
            )
            elapsed_ms = int((time.perf_counter() - request_started) * 1000)
            log_line(
                RUN_LOG_PATH,
                f"openai_request_done request_id={request_id} mode=legacy elapsed_ms={elapsed_ms}",
            )
            return (resp.choices[0].message.content or "").strip()

        try:
            if conversation_id and not conversation_failure:
                try:
                    log_line(
                        RUN_LOG_PATH,
                        f"conversation_use request_id={request_id} conversation={conversation_id}",
                    )
                    reply_text = request_openai(use_thread=True)
                except Exception as exc:
                    if is_conversation_invalid(exc):
                        log_error(
                            f"conversation_invalid request_id={request_id} "
                            f"conversation={conversation_id} error={exc}"
                        )
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
                            log_error(
                                f"conversation_fallback_stateless request_id={request_id} avatar={avatar_key}"
                            )
                            reply_text = request_openai(use_thread=False)
                    else:
                        raise
            else:
                log_line(
                    RUN_LOG_PATH,
                    f"conversation_bypass request_id={request_id} reason={'failure' if conversation_failure else 'disabled'}",
                )
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
            endpoint,
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
            endpoint,
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            status_code,
            response_payload,
        )
        return response_payload, status_code
    except Exception as exc:
        log_unhandled_exception(request_id, exc)
        error_message = safe_error_reason(exc)
        reply = f"Error: server_exception ({error_message})."
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            endpoint,
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
            endpoint,
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            502,
            response_payload,
        )
        return response_payload, 502


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
    has_first_conversation_prompt = "first_conversation_prompt" in data
    first_conversation_prompt = data.get("first_conversation_prompt") or ""

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
    if not isinstance(first_conversation_prompt, str):
        first_conversation_prompt = str(first_conversation_prompt)

    display_name = (data.get("display_name") or "").strip()
    model = (data.get("model") or "").strip()
    max_history_raw = data.get("max_history_events")

    profile_dir = npc_profile_dir(npc_id)
    ensure_dir(profile_dir)
    atomic_write_text(npc_system_path(npc_id), system_prompt)
    if has_first_conversation_prompt:
        atomic_write_text(
            npc_first_conversation_path(npc_id), first_conversation_prompt
        )

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


@app.post("/sl/callback/register")
def sl_callback_register() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/sl/callback/register",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line(
            "/sl/callback/register", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    object_key = (data.get("object_key") or "").strip()
    callback_url = (data.get("callback_url") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    region = (data.get("region") or "").strip()

    log_incoming_request(
        "/sl/callback/register",
        request_id,
        "",
        "",
        npc_id,
        data,
        raw_body,
        note="received",
    )

    if not object_key or not callback_url:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/sl/callback/register",
            request_id,
            "",
            "",
            npc_id,
            "",
            "400",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "missing_fields", "request_id": request_id}, 400
        )

    if not (callback_url.startswith("http://") or callback_url.startswith("https://")):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/sl/callback/register",
            request_id,
            "",
            "",
            npc_id,
            "",
            "400",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "invalid_callback_url", "request_id": request_id},
            400,
        )

    callback_entry = get_callback(object_key, npc_id)
    if (
        callback_entry
        and callback_entry.get("url") == callback_url
        and callback_entry.get("token")
    ):
        callback_token = callback_entry["token"]
    else:
        callback_token = make_callback_token()
    set_callback(object_key, npc_id, callback_url, callback_token, region)

    response_payload = {
        "ok": True,
        "callback_token": callback_token,
        "expires_sec": CALLBACK_TTL_SECONDS,
    }
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/sl/callback/register",
        request_id,
        "",
        "",
        npc_id,
        "",
        "200",
        elapsed_ms,
    )
    log_response_payload(
        "/sl/callback/register", request_id, "", "", npc_id, 200, response_payload
    )
    return json_response(response_payload, 200)


@app.get("/sl/fetch")
def sl_fetch() -> Response:
    token = (request.args.get("token") or "").strip()
    if not token:
        return Response("missing_token", status=400, mimetype="text/plain")
    body = pkg_cache_get(token)
    if not body:
        return Response("not_found", status=404, mimetype="text/plain")
    return Response(body, status=200, mimetype="text/plain")


@app.post("/pool/register")
def pool_register() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/pool/register",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line(
            "/pool/register", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    object_id = (data.get("object_id") or "").strip()
    object_key = (data.get("object_key") or "").strip()
    object_name = (data.get("object_name") or "").strip()
    region = (data.get("region") or "").strip()
    position = (data.get("position") or "").strip()
    difficulty = data.get("difficulty", 1)
    hint = (data.get("hint") or "").strip()
    found_message = (data.get("found_message") or "").strip()
    category = (data.get("category") or "hidden").strip()

    log_incoming_request(
        "/pool/register",
        request_id,
        "",
        "",
        "",
        data,
        raw_body,
        note="received",
    )

    if not object_id:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/pool/register", request_id, "", "", "", "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "missing_object_id", "request_id": request_id}, 400
        )

    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        difficulty = 1

    with POOL_LOCK:
        pool = load_pool()
        objects = pool.setdefault("objects", {})
        objects[object_id] = {
            "object_id": object_id,
            "object_key": object_key,
            "object_name": object_name,
            "region": region,
            "position": position,
            "difficulty": difficulty,
            "hint": hint,
            "found_message": found_message,
            "category": category,
            "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_pool(pool)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/pool/register", request_id, "", "", "", object_id, "200", elapsed_ms
    )
    response_payload = {"ok": True, "object_id": object_id, "registered": True}
    log_response_payload(
        "/pool/register", request_id, "", "", "", 200, response_payload
    )
    return json_response(response_payload, 200)


@app.get("/pool/status")
def pool_status() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]

    pool = load_pool()
    total_objects = len(pool.get("objects", {}))
    active_objects_list = get_active_objects()
    active_count = len(active_objects_list)

    by_difficulty: dict[int, int] = {}
    by_category: dict[str, int] = {}
    for obj in active_objects_list:
        diff = obj.get("difficulty", 1)
        by_difficulty[diff] = by_difficulty.get(diff, 0) + 1
        cat = obj.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line("/pool/status", request_id, "", "", "", "", "200", elapsed_ms)

    response_payload = {
        "ok": True,
        "total_objects": total_objects,
        "active_objects": active_count,
        "by_difficulty": by_difficulty,
        "by_category": by_category,
    }
    return json_response(response_payload, 200)


@app.post("/quest/event")
def quest_event() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/quest/event",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line("/quest/event", request_id, "", "", "", "", "400", elapsed_ms)
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    avatar_key = (data.get("avatar_key") or "").strip()
    object_id = (data.get("object_id") or "").strip()
    event = (data.get("event") or "").strip()

    # Legacy support: object_key can be used if object_id not provided
    if not object_id:
        object_id = (data.get("object_key") or "").strip()

    # Legacy support: quest_id field (ignore it, use object_id)
    quest_id = (data.get("quest_id") or "").strip()

    if not avatar_key or not event:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/quest/event",
            request_id,
            "",
            avatar_key,
            "",
            data,
            raw_body,
            note="missing_fields",
        )
        log_request_line("/quest/event", request_id, "", avatar_key, "", "", "400", elapsed_ms)
        return json_response(
            {"ok": False, "error": "missing_fields", "request_id": request_id}, 400
        )

    log_incoming_request(
        "/quest/event",
        request_id,
        "",
        avatar_key,
        "",
        data,
        raw_body,
        note="received",
    )

    # Handle event based on type
    if event == "object_found":
        result = QuestEngine.handle_quest_event(avatar_key, object_id)
    else:
        # Legacy event handling (cube_clicked, etc.)
        result = QuestEngine.quest_handle_event(
            avatar_key, quest_id or "dynamic", event, {"object_id": object_id, "object_key": object_id}
        )

    # Build quest context for conversation update
    quest_context = QuestEngine.build_quest_context(avatar_key)

    # Try to update any active conversations with quest status
    # Note: We don't have npc_id in this context, so we can't target a specific conversation

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line("/quest/event", request_id, "", avatar_key, "", event, "200", elapsed_ms)

    response_payload = {
        "ok": True,
        "matched": result.get("matched", False),
        "quest_completed": result.get("quest_completed", False),
        "found_count": result.get("found_count"),
        "total_count": result.get("total_count"),
    }
    log_response_payload(
        "/quest/event", request_id, "", avatar_key, "", 200, response_payload
    )
    return json_response(response_payload, 200)


@app.post("/chat_async")
def chat_async() -> tuple[Response, int]:
    start_time = time.perf_counter()
    request_id = uuid4().hex[:8]
    raw_body = request.get_data(cache=True, as_text=True) or ""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_incoming_request(
            "/chat_async",
            request_id,
            "",
            "",
            "",
            data,
            raw_body,
            note="invalid_json",
        )
        log_request_line("/chat_async", request_id, "", "", "", "", "400", elapsed_ms)
        return json_response(
            {"ok": False, "error": "invalid_json", "request_id": request_id}, 400
        )

    message = (data.get("message") or "").strip()
    avatar_key = (data.get("avatar_key") or "").strip()
    object_key = (data.get("object_key") or "").strip()
    npc_id = (data.get("npc_id") or "SLQuest_DefaultNPC").strip()
    client_req_id = (data.get("client_req_id") or "").strip()
    callback_token = (data.get("callback_token") or "").strip()

    log_incoming_request(
        "/chat_async",
        request_id,
        client_req_id,
        avatar_key,
        npc_id,
        data,
        raw_body,
        note="received",
    )

    if not message:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat_async", request_id, client_req_id, avatar_key, npc_id, "", "400", elapsed_ms
        )
        return json_response(
            {"ok": False, "error": "message_required", "request_id": request_id}, 400
        )

    if not avatar_key or not object_key or not npc_id or not callback_token:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat_async",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "400",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "missing_fields", "request_id": request_id}, 400
        )

    if not client_req_id:
        client_req_id = uuid4().hex
        data["client_req_id"] = client_req_id

    callback_entry = get_callback(object_key, npc_id)
    if not callback_entry:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat_async",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "409",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "callback_not_registered", "request_id": request_id},
            409,
        )

    stored_token = (callback_entry.get("token") or "").strip()
    if stored_token != callback_token:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        log_request_line(
            "/chat_async",
            request_id,
            client_req_id,
            avatar_key,
            npc_id,
            message,
            "403",
            elapsed_ms,
        )
        return json_response(
            {"ok": False, "error": "callback_token_invalid", "request_id": request_id},
            403,
        )

    response_payload = {
        "ok": True,
        "queued": True,
        "client_req_id": client_req_id,
    }
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    log_request_line(
        "/chat_async",
        request_id,
        client_req_id,
        avatar_key,
        npc_id,
        message,
        "200",
        elapsed_ms,
    )
    log_response_payload(
        "/chat_async", request_id, client_req_id, avatar_key, npc_id, 200, response_payload
    )

    callback_url = (callback_entry.get("url") or "").strip()
    callback_token = stored_token
    callback_request_id = uuid4().hex[:8]
    worker_data = dict(data)

    def worker() -> None:
        raw_message = worker_data.get("message", "")
        avatar_uuid = worker_data.get("avatar_key", "")
        npc_name = worker_data.get("npc_id", "")
        log_line(
            RUN_LOG_PATH,
            "chat_async_worker_start "
            f"request_id={callback_request_id} avatar={avatar_uuid or '-'} npc_id={npc_name or '-'} "
            f"callback_url={redact_callback_url(callback_url)}",
        )
        pre = {}
        try:
            pre = QuestEngine.quest_pre_chat(avatar_uuid, npc_name, raw_message)
        except Exception as exc:
            log_error(f"quest_pre_chat_failed avatar={avatar_uuid} error={exc}")
        worker_data["quest_context"] = pre.get("quest_context", "")

        payload, _status = run_chat_logic(
            "/chat_async", callback_request_id, worker_data, raw_body=""
        )
        pkg_cache_prune()

        post = {}
        try:
            post = QuestEngine.quest_post_chat(avatar_uuid, npc_name, raw_message)
        except Exception as exc:
            log_error(f"quest_post_chat_failed avatar={avatar_uuid} error={exc}")

        chat_text = (payload.get("reply") or "").strip()
        if not chat_text:
            force_reply = post.get("force_reply")
            if isinstance(force_reply, str):
                chat_text = force_reply.strip()

        actions = []
        if isinstance(pre.get("actions"), list):
            actions.extend([action for action in pre["actions"] if isinstance(action, str)])
        if isinstance(post.get("actions"), list):
            actions.extend([action for action in post["actions"] if isinstance(action, str)])

        quest_pack = post.get("quest") if isinstance(post.get("quest"), dict) else None
        if not quest_pack and isinstance(pre.get("quest"), dict):
            quest_pack = pre.get("quest")
        qpack = pack_quest(quest_pack) if quest_pack else ""

        ok = bool(payload.get("ok", False))
        err = (payload.get("error") or "").strip()
        act_str = pack_actions(actions)
        pkg_body = pack(
            [
                ("V", "1"),
                ("TYPE", "PKG"),
                ("RID", worker_data.get("client_req_id", "")),
                ("USER", worker_data.get("avatar_key", "")),
                ("NPC", worker_data.get("npc_id", "")),
                ("OK", "1" if ok else "0"),
                ("CHAT", chat_text),
                ("ACT", act_str),
                ("Q", qpack),
                ("CB", callback_token),
                ("ERR", err),
            ]
        )

        if len(pkg_body) <= SAFE_CALLBACK_MAX:
            cb_body = pkg_body
        else:
            fetch_token = pkg_cache_put(pkg_body)
            cb_body = pack(
                [
                    ("V", "1"),
                    ("TYPE", "FETCH"),
                    ("RID", worker_data.get("client_req_id", "")),
                    ("USER", worker_data.get("avatar_key", "")),
                    ("NPC", worker_data.get("npc_id", "")),
                    ("OK", "1" if ok else "0"),
                    ("TOKEN", fetch_token),
                    ("CB", callback_token),
                    ("ERR", err),
                ]
            )

        log_line(
            RUN_LOG_PATH,
            "chat_async_worker_payload "
            f"request_id={callback_request_id} ok={int(ok)} pkg_chars={len(pkg_body)} "
            f"callback_mode={'direct' if cb_body == pkg_body else 'fetch'}",
        )
        success, error = post_callback(callback_url, callback_token, cb_body)
        if success:
            log_line(
                RUN_LOG_PATH,
                f"callback_post_ok request_id={callback_request_id} avatar={worker_data.get('avatar_key','')} npc_id={worker_data.get('npc_id','')}",
            )
        else:
            log_error(
                f"callback_post_failed request_id={callback_request_id} avatar={worker_data.get('avatar_key','')} npc_id={worker_data.get('npc_id','')} error={error}"
            )

    threading.Thread(target=worker, daemon=True).start()
    return json_response(response_payload, 200)


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
    response_payload, status_code = run_chat_logic("/chat", request_id, data, raw_body)
    return json_response(response_payload, status_code)


if __name__ == "__main__":
    from waitress import serve

    ensure_dir(LOGS_ROOT)
    ensure_dir(CHAT_ROOT)
    load_callbacks()
    log_startup_status()
    serve(app, host="0.0.0.0", port=PORT)
