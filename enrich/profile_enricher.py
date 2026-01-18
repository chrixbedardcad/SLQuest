from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_ROOT = BASE_DIR / "state"
LOGS_ROOT = BASE_DIR / "logs"

PROFILE_CARD_TTL_DAYS = int(os.getenv("PROFILE_CARD_TTL_DAYS", "7"))
PROFILE_IMAGE_ENABLED = (os.getenv("PROFILE_IMAGE_ENABLED", "0").strip() == "1")
PROFILE_IMAGE_URL_TEMPLATE = (os.getenv("PROFILE_IMAGE_URL_TEMPLATE") or "").strip()
CORRADE_PROFILE_ENDPOINT = (os.getenv("CORRADE_PROFILE_ENDPOINT") or "").strip()
CORRADE_API_KEY = (os.getenv("CORRADE_API_KEY") or "").strip()
CORRADE_TIMEOUT_SECONDS = float(os.getenv("CORRADE_TIMEOUT_SECONDS", "4.0"))

STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "among",
    "and",
    "another",
    "around",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "came",
    "come",
    "could",
    "does",
    "each",
    "either",
    "from",
    "have",
    "here",
    "into",
    "just",
    "like",
    "more",
    "most",
    "much",
    "must",
    "near",
    "only",
    "other",
    "over",
    "said",
    "same",
    "since",
    "some",
    "such",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "too",
    "upon",
    "very",
    "were",
    "what",
    "when",
    "where",
    "which",
    "will",
    "with",
    "your",
}


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def log_line(message: str) -> None:
    ensure_dir(LOGS_ROOT)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_path = LOGS_ROOT / "profile_enricher.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def state_avatar_dir(avatar_uuid: str) -> Path:
    return STATE_ROOT / avatar_uuid


def profile_card_path(avatar_uuid: str) -> Path:
    return state_avatar_dir(avatar_uuid) / "profile_card.json"


def parse_last_updated(card: dict[str, Any]) -> datetime | None:
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


def is_card_fresh(card: dict[str, Any], ttl_days: int) -> bool:
    last_updated = parse_last_updated(card)
    if not last_updated:
        return False
    return datetime.now(timezone.utc) - last_updated < timedelta(days=ttl_days)


def load_profile_card(avatar_uuid: str) -> dict[str, Any] | None:
    path = profile_card_path(avatar_uuid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_profile_card(avatar_uuid: str, card: dict[str, Any]) -> None:
    path = profile_card_path(avatar_uuid)
    ensure_dir(path.parent)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def tokenize_keywords(text: str) -> list[str]:
    tokens = []
    for raw in re.split(r"[^A-Za-z0-9]+", text.lower()):
        if len(raw) < 3:
            continue
        if raw in STOPWORDS:
            continue
        tokens.append(raw)
    return tokens


def extract_keywords(*texts: str, limit: int = 8) -> list[str]:
    combined = " ".join(t for t in texts if t)
    if not combined.strip():
        return []
    counts = Counter(tokenize_keywords(combined))
    if not counts:
        return []
    keywords = [word for word, _ in counts.most_common(limit)]
    return keywords


def normalize_username(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered.endswith(" resident"):
        return value[: -len(" resident")].strip()
    return value


def fetch_corrade_profile(avatar_uuid: str) -> dict[str, Any]:
    if not CORRADE_PROFILE_ENDPOINT:
        log_line(f"corrade_profile_skipped avatar={avatar_uuid} reason=no_endpoint")
        return {}
    log_line(f"corrade_profile_start avatar={avatar_uuid}")
    payload = {
        "avatar_uuid": avatar_uuid,
        "fields": ["display_name", "about", "interests", "profile_text"],
    }
    headers = {"Content-Type": "application/json"}
    if CORRADE_API_KEY:
        headers["Authorization"] = f"Bearer {CORRADE_API_KEY}"
    request = Request(
        CORRADE_PROFILE_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        start_time = time.perf_counter()
        with urlopen(request, timeout=CORRADE_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", "unknown")
            log_line(
                f"corrade_profile_ok avatar={avatar_uuid} status={status_code} elapsed_ms={elapsed_ms}"
            )
    except (URLError, HTTPError, TimeoutError) as exc:
        log_line(f"corrade_profile_failed avatar={avatar_uuid} error={exc}")
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log_line(f"corrade_profile_invalid_json avatar={avatar_uuid}")
        return {}
    if not isinstance(data, dict):
        return {}
    log_line(
        f"corrade_profile_parsed avatar={avatar_uuid} keys={len(list(data.keys()))}"
    )
    return data


def fetch_image_uuid_from_web(avatar_uuid: str, username: str = "") -> str | None:
    if username:
        url = f"https://my.secondlife.com/{username}"
    else:
        url = f"https://world.secondlife.com/resident/{avatar_uuid}"
    request = Request(url, headers={"User-Agent": "SLQuestProfileEnricher/1.0"})
    try:
        start_time = time.perf_counter()
        with urlopen(request, timeout=4.0) as response:
            html = response.read().decode("utf-8", errors="ignore")
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", "unknown")
            log_line(
                f"web_profile_ok avatar={avatar_uuid} status={status_code} elapsed_ms={elapsed_ms}"
            )
    except (URLError, HTTPError, TimeoutError) as exc:
        log_line(f"web_profile_fetch_failed avatar={avatar_uuid} error={exc}")
        return None
    match = re.search(r"meta\s+name=\"imageid\"\s+content=\"([A-Fa-f0-9-]{36})\"", html)
    if not match:
        return None
    return match.group(1)


def download_profile_image(image_uuid: str) -> bytes | None:
    if not PROFILE_IMAGE_URL_TEMPLATE:
        log_line(f"profile_image_skipped image_uuid={image_uuid} reason=no_template")
        return None
    url = PROFILE_IMAGE_URL_TEMPLATE.format(image_uuid=image_uuid)
    request = Request(url, headers={"User-Agent": "SLQuestProfileEnricher/1.0"})
    try:
        start_time = time.perf_counter()
        with urlopen(request, timeout=5.0) as response:
            payload = response.read()
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            status_code = getattr(response, "status", "unknown")
            log_line(
                f"profile_image_ok image_uuid={image_uuid} status={status_code} bytes={len(payload)} elapsed_ms={elapsed_ms}"
            )
            return payload
    except (URLError, HTTPError, TimeoutError) as exc:
        log_line(f"profile_image_download_failed image_uuid={image_uuid} error={exc}")
        return None


def vibe_tags_from_image_bytes(image_bytes: bytes | None) -> list[str]:
    if not image_bytes:
        return []
    if len(image_bytes) < 10:
        return []
    return []


def build_safe_personalization(keywords: list[str], vibe_tags: list[str]) -> dict[str, Any]:
    topics = []
    if keywords:
        topics.extend(keywords[:3])
    if vibe_tags:
        topics.extend(vibe_tags[:2])
    return {
        "greeting_style": "friendly-short",
        "topics_to_offer": topics,
        "tone_avoid": [
            "real-life identity",
            "appearance judgments",
            "romance unless initiated",
        ],
    }


def build_profile_card(
    avatar_uuid: str,
    avatar_name: str = "",
    avatar_display_name: str = "",
    avatar_username: str = "",
) -> dict[str, Any]:
    log_line(f"enrich_start avatar={avatar_uuid}")
    corrade_data = fetch_corrade_profile(avatar_uuid)
    display_name = (
        (corrade_data.get("display_name") or "").strip() if isinstance(corrade_data, dict) else ""
    )
    avatar_name = (avatar_name or "").strip()
    avatar_display_name = (avatar_display_name or "").strip()
    avatar_username = normalize_username(avatar_username or avatar_name)
    username = ""
    if isinstance(corrade_data, dict):
        for key in ("username", "legacy_name", "name", "avatar_name"):
            value = corrade_data.get(key)
            if isinstance(value, str) and value.strip():
                username = value.strip()
                break
    if not username and avatar_username:
        username = avatar_username
    if not username:
        username = avatar_name
    if not display_name and avatar_display_name:
        display_name = avatar_display_name
    if not display_name and username:
        display_name = username
    about_text = ""
    interests_text = ""
    if isinstance(corrade_data, dict):
        about_text = str(corrade_data.get("about") or corrade_data.get("profile_text") or "")
        interests_text = str(corrade_data.get("interests") or "")
    keywords = extract_keywords(about_text, interests_text)
    log_line(
        f"keyword_extract avatar={avatar_uuid} about_len={len(about_text)} interests_len={len(interests_text)} keywords={len(keywords)}"
    )

    image_uuid = None
    image_analyzed = False
    image_vibe_tags: list[str] = []
    web_profile_used = False
    if PROFILE_IMAGE_ENABLED:
        if isinstance(corrade_data, dict):
            for key in ("image_uuid", "profile_image_uuid", "imageid", "profile_image_id"):
                value = corrade_data.get(key)
                if isinstance(value, str) and value.strip():
                    image_uuid = value.strip()
                    break
        if not image_uuid:
            image_uuid = fetch_image_uuid_from_web(avatar_uuid, username=username)
            web_profile_used = bool(image_uuid)
        if image_uuid:
            image_bytes = download_profile_image(image_uuid)
            image_vibe_tags = vibe_tags_from_image_bytes(image_bytes)
            image_analyzed = bool(image_bytes)
            log_line(
                f"image_vibe_tags avatar={avatar_uuid} tags={len(image_vibe_tags)} analyzed={int(image_analyzed)}"
            )
    else:
        log_line(f"profile_image_disabled avatar={avatar_uuid}")

    card = {
        "avatar_uuid": avatar_uuid,
        "display_name": display_name or "Unknown",
        "username": username or "Unknown",
        "profile_keywords": keywords,
        "image_vibe_tags": image_vibe_tags,
        "safe_personalization": build_safe_personalization(keywords, image_vibe_tags),
        "source_notes": {
            "corrade_profiledata": bool(corrade_data),
            "lsl_avatar_name_used": bool(avatar_name),
            "lsl_display_name_used": bool(avatar_display_name),
            "lsl_username_used": bool(avatar_username),
            "web_profile_used": web_profile_used,
            "image_analyzed": image_analyzed,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    log_line(f"enrich_complete avatar={avatar_uuid} keywords={len(keywords)}")
    return card


def get_or_create_profile_card(
    avatar_uuid: str,
    force: bool = False,
    avatar_name: str = "",
    avatar_display_name: str = "",
    avatar_username: str = "",
) -> dict[str, Any]:
    existing = load_profile_card(avatar_uuid)
    if existing and not force and is_card_fresh(existing, PROFILE_CARD_TTL_DAYS):
        log_line(f"cache_hit avatar={avatar_uuid}")
        return existing
    if existing:
        log_line(
            f"cache_stale avatar={avatar_uuid} ttl_days={PROFILE_CARD_TTL_DAYS}"
        )
    else:
        log_line(f"cache_miss avatar={avatar_uuid} ttl_days={PROFILE_CARD_TTL_DAYS}")
    card = build_profile_card(
        avatar_uuid,
        avatar_name=avatar_name,
        avatar_display_name=avatar_display_name,
        avatar_username=avatar_username,
    )
    save_profile_card(avatar_uuid, card)
    return card


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build a profile card for an avatar.")
    parser.add_argument("avatar_uuid", help="Avatar UUID to enrich")
    parser.add_argument("--force", action="store_true", help="Force refresh even if cached")
    args = parser.parse_args()
    card = get_or_create_profile_card(args.avatar_uuid, force=args.force)
    print(json.dumps(card, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
