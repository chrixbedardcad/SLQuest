from __future__ import annotations

import base64
import html
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

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_ROOT = BASE_DIR / "state"
LOGS_ROOT = BASE_DIR / "logs"

PROFILE_CARD_TTL_DAYS = int(os.getenv("PROFILE_CARD_TTL_DAYS", "7"))
PROFILE_IMAGE_URL_TEMPLATE = (os.getenv("PROFILE_IMAGE_URL_TEMPLATE") or "").strip()
_PROFILE_IMAGE_ENABLED_ENV = os.getenv("PROFILE_IMAGE_ENABLED")
PROFILE_VISION_ENABLED = (os.getenv("PROFILE_VISION_ENABLED") or "0").strip() == "1"
PROFILE_VISION_MODEL = (os.getenv("PROFILE_VISION_MODEL") or "gpt-4.1-mini").strip()
PROFILE_VISION_MAX_TOKENS = int(os.getenv("PROFILE_VISION_MAX_TOKENS", "350"))
if _PROFILE_IMAGE_ENABLED_ENV is None:
    PROFILE_IMAGE_ENABLED = bool(PROFILE_IMAGE_URL_TEMPLATE)
else:
    PROFILE_IMAGE_ENABLED = _PROFILE_IMAGE_ENABLED_ENV.strip() == "1"

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_API_BASE = (os.getenv("OPENAI_API_BASE") or "https://api.openai.com").strip()
OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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


def profile_detail_path(avatar_uuid: str) -> Path:
    return state_avatar_dir(avatar_uuid) / "profile_detail.txt"


def profile_image_path(avatar_uuid: str, extension: str) -> Path:
    safe_extension = extension.lstrip(".") or "bin"
    return state_avatar_dir(avatar_uuid) / f"profile_image.{safe_extension}"


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


def save_profile_detail(avatar_uuid: str, detail_text: str) -> None:
    path = profile_detail_path(avatar_uuid)
    ensure_dir(path.parent)
    path.write_text(detail_text, encoding="utf-8")


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


def build_profile_url(avatar_uuid: str, username: str = "") -> str:
    if username:
        safe_username = username.strip().strip("/")
        return f"https://my.secondlife.com/{safe_username}/"
    return f"https://world.secondlife.com/resident/{avatar_uuid}"


def fetch_profile_html(avatar_uuid: str, url: str) -> str | None:
    log_line(f"web_profile_start avatar={avatar_uuid} url={url}")
    request = Request(url, headers={"User-Agent": "SLQuestProfileEnricher/1.0"})
    for attempt in range(2):
        try:
            start_time = time.perf_counter()
            with urlopen(request, timeout=4.0) as response:
                html = response.read().decode("utf-8", errors="ignore")
                elapsed_ms = int((time.perf_counter() - start_time) * 1000)
                status_code = getattr(response, "status", "unknown")
                log_line(
                    f"web_profile_ok avatar={avatar_uuid} status={status_code} elapsed_ms={elapsed_ms}"
                )
                return html
        except (URLError, HTTPError, TimeoutError) as exc:
            if attempt == 0:
                log_line(
                    f"web_profile_retry avatar={avatar_uuid} error={type(exc).__name__}:{exc}"
                )
                continue
            log_line(f"web_profile_fetch_failed avatar={avatar_uuid} error={exc}")
            return None


def strip_html_tags(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return html_unescape(cleaned).strip()


def extract_biography(html_text: str) -> str:
    match = re.search(
        r"id=[\"']sl_about_text[\"'][^>]*>(.*?)</div>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return strip_html_tags(match.group(1))


def fetch_web_profile(avatar_uuid: str, username: str = "") -> dict[str, Any]:
    url = build_profile_url(avatar_uuid, username=username)
    html_text = fetch_profile_html(avatar_uuid, url)
    if html_text is None and username:
        fallback_url = build_profile_url(avatar_uuid, username="")
        log_line(f"web_profile_retry avatar={avatar_uuid} url={fallback_url}")
        html_text = fetch_profile_html(avatar_uuid, fallback_url)
        url = fallback_url
    if html_text is None:
        return {}
    image_match = re.search(
        r"meta\s+name=\"imageid\"\s+content=\"([A-Fa-f0-9-]{36})\"",
        html_text,
        flags=re.IGNORECASE,
    )
    description_match = re.search(
        r"meta\s+name=\"description\"\s+content=\"([^\"]*)\"",
        html_text,
        flags=re.IGNORECASE,
    )
    title_match = re.search(
        r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL
    )
    display_name = html_unescape(title_match.group(1)).strip() if title_match else ""
    description = html_unescape(description_match.group(1)).strip() if description_match else ""
    biography = extract_biography(html_text)
    if biography:
        description = biography
    image_uuid = image_match.group(1) if image_match else None
    if not image_uuid:
        log_line(f"web_profile_no_imageid avatar={avatar_uuid}")
    return {
        "url": url,
        "display_name": display_name,
        "description": description,
        "image_uuid": image_uuid,
    }


def html_unescape(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    return html.unescape(value).strip()


def download_profile_image(
    image_uuid: str | None, username: str
) -> tuple[bytes | None, str | None, str | None]:
    log_line(
        "profile_image_download_start "
        f"image_uuid={image_uuid or 'none'} username={username or 'none'} "
        f"template_set={int(bool(PROFILE_IMAGE_URL_TEMPLATE))}"
    )
    if not PROFILE_IMAGE_URL_TEMPLATE:
        log_line(f"profile_image_skipped image_uuid={image_uuid} reason=no_template")
        return None, None, None
    if "{image_uuid}" in PROFILE_IMAGE_URL_TEMPLATE and not image_uuid:
        log_line("profile_image_skipped reason=missing_image_uuid")
        return None, None, None
    if "{username}" in PROFILE_IMAGE_URL_TEMPLATE and not username:
        log_line("profile_image_skipped reason=missing_username")
        return None, None, None
    url = PROFILE_IMAGE_URL_TEMPLATE.format(image_uuid=image_uuid or "", username=username)
    log_line(f"profile_image_request image_uuid={image_uuid} url={url}")
    request = Request(url, headers={"User-Agent": "SLQuestProfileEnricher/1.0"})
    for attempt in range(2):
        try:
            start_time = time.perf_counter()
            with urlopen(request, timeout=5.0) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
                elapsed_ms = int((time.perf_counter() - start_time) * 1000)
                status_code = getattr(response, "status", "unknown")
                log_line(
                    "profile_image_ok "
                    f"image_uuid={image_uuid} status={status_code} bytes={len(payload)} "
                    f"elapsed_ms={elapsed_ms} content_type={content_type or 'unknown'}"
                )
                return payload, content_type, url
        except HTTPError as exc:
            if attempt == 0 and exc.code >= 500:
                log_line(
                    "profile_image_retry "
                    f"image_uuid={image_uuid} status={exc.code} reason={exc.reason}"
                )
                continue
            log_line(
                "profile_image_download_failed "
                f"image_uuid={image_uuid} status={exc.code} reason={exc.reason}"
            )
            return None, None, url
        except (URLError, TimeoutError) as exc:
            if attempt == 0:
                log_line(
                    "profile_image_retry "
                    f"image_uuid={image_uuid} error={type(exc).__name__}:{exc}"
                )
                continue
            log_line(
                "profile_image_download_failed "
                f"image_uuid={image_uuid} error={type(exc).__name__}:{exc}"
            )
            return None, None, url


def summarize_about_text(about_text: str, limit: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", about_text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    truncated = cleaned[:limit].rstrip()
    return truncated + "…"


def build_profile_image_url(image_uuid: str | None, username: str) -> str | None:
    if not PROFILE_IMAGE_URL_TEMPLATE:
        return None
    if "{image_uuid}" in PROFILE_IMAGE_URL_TEMPLATE and not image_uuid:
        return None
    if "{username}" in PROFILE_IMAGE_URL_TEMPLATE and not username:
        return None
    return PROFILE_IMAGE_URL_TEMPLATE.format(
        image_uuid=image_uuid or "", username=username
    )


def build_image_input_payload(
    image_url: str | None, image_bytes: bytes | None, content_type: str | None
) -> dict[str, Any] | None:
    if image_url:
        return {"type": "input_image", "image_url": image_url}
    if image_bytes:
        mime_type = content_type or "image/png"
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"}
    return None


def analyze_profile_image(
    avatar_uuid: str,
    image_url: str | None,
    image_bytes: bytes | None,
    content_type: str | None,
) -> dict[str, Any] | None:
    if not PROFILE_VISION_ENABLED:
        return None
    if not OPENAI_CLIENT:
        log_line(f"profile_vision_disabled avatar={avatar_uuid} reason=missing_api_key")
        return None
    image_payload = build_image_input_payload(image_url, None, None)
    fallback_payload = build_image_input_payload(None, image_bytes, content_type)
    if not image_payload and not fallback_payload:
        log_line(f"profile_vision_skipped avatar={avatar_uuid} reason=no_image")
        return None
    prompt = (
        "You are analyzing a Second Life avatar profile picture. "
        "Return STRICT JSON matching this schema:\n"
        "{\n"
        '  "source_url": "...",\n'
        '  "summary": "one paragraph safe description of avatar styling",\n'
        '  "style_tags": ["..."],\n'
        '  "hair": {"color":"", "length":"", "style":""},\n'
        '  "eyes": {"color":"", "notes":""},\n'
        '  "accessories": ["..."],\n'
        '  "clothing_summary": "",\n'
        '  "nonhuman_traits": ["..."],\n'
        '  "vibe_keywords": ["..."],\n'
        '  "safe_hooks": ["..."],\n'
        '  "avoid_assumptions": ["no gender/sex identity", "no age", "no ethnicity", "no real identity"],\n'
        '  "confidence": {"hair_color":0.0,"eye_color":0.0,"style_tags":0.0,"accessories":0.0}\n'
        "}\n"
        "Rules: Do not infer sensitive traits (gender/sex identity, age, ethnicity, nationality, "
        "real identity). Focus only on safe avatar styling: hair/eyes/style/accessories, "
        "species cues, outfit, colors, vibe keywords, and safe conversation hooks."
    )
    for attempt in range(2):
        try:
            response = OPENAI_CLIENT.responses.create(
                model=PROFILE_VISION_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            image_payload or fallback_payload,
                        ],
                    }
                ],
                max_output_tokens=PROFILE_VISION_MAX_TOKENS,
            )
            raw_text = (response.output_text or "").strip()
            if not raw_text:
                log_line(f"profile_vision_empty avatar={avatar_uuid}")
                return None
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                log_line(f"profile_vision_invalid avatar={avatar_uuid} reason=non_dict")
                return None
            parsed.setdefault("source_url", image_url or "")
            return parsed
        except Exception as exc:
            if attempt == 0:
                if image_payload and fallback_payload:
                    log_line(
                        f"profile_vision_retry avatar={avatar_uuid} error={type(exc).__name__}:{exc}"
                    )
                    image_payload = None
                    continue
            log_line(f"profile_vision_failed avatar={avatar_uuid} error={exc}")
            return None


def vibe_tags_from_image_bytes(image_bytes: bytes | None) -> list[str]:
    if not image_bytes:
        return []
    if len(image_bytes) < 10:
        return []
    return []


def build_safe_personalization(
    keywords: list[str],
    vibe_tags: list[str],
    safe_hooks: list[str] | None = None,
) -> dict[str, Any]:
    topics = []
    if keywords:
        topics.extend(keywords[:3])
    if vibe_tags:
        topics.extend(vibe_tags[:2])
    hooks = [item for item in (safe_hooks or []) if item][:4]
    return {
        "greeting_style": "friendly-short",
        "topics_to_offer": topics,
        "safe_hooks": hooks,
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
    avatar_name = (avatar_name or "").strip()
    avatar_display_name = (avatar_display_name or "").strip()
    avatar_username = normalize_username(avatar_username or avatar_name)
    username = ""
    if not username and avatar_username:
        username = avatar_username
    if not username:
        username = avatar_name
    web_profile = fetch_web_profile(avatar_uuid, username=avatar_username)
    display_name = (web_profile.get("display_name") or "").strip()
    if not display_name and avatar_display_name:
        display_name = avatar_display_name
    if not display_name and username:
        display_name = username
    log_line(
        "enrich_identity "
        f"avatar={avatar_uuid} username={username or 'Unknown'} display_name={display_name or 'Unknown'} "
        f"web_profile={int(bool(web_profile))} lsl_username={int(bool(avatar_username))} "
        f"lsl_display_name={int(bool(avatar_display_name))}"
    )
    about_text = ""
    interests_text = ""
    if isinstance(web_profile, dict):
        about_text = str(web_profile.get("description") or "")
    about_summary = summarize_about_text(about_text)
    keywords = extract_keywords(about_text, about_summary, interests_text)
    log_line(
        f"keyword_extract avatar={avatar_uuid} about_len={len(about_text)} interests_len={len(interests_text)} keywords={len(keywords)}"
    )

    image_uuid = None
    image_analyzed = False
    image_vibe_tags: list[str] = []
    visual_profile: dict[str, Any] | None = None
    image_bytes: bytes | None = None
    content_type: str | None = None
    image_url: str | None = None
    web_profile_used = bool(web_profile)
    if isinstance(web_profile, dict):
        value = web_profile.get("image_uuid")
        if isinstance(value, str) and value.strip():
            image_uuid = value.strip()
        else:
            log_line(f"profile_image_no_uuid avatar={avatar_uuid}")
    else:
        log_line(f"profile_image_no_profile avatar={avatar_uuid}")

    if PROFILE_IMAGE_ENABLED:
        template_uses_username = "{username}" in PROFILE_IMAGE_URL_TEMPLATE
        log_line(
            "profile_image_config "
            f"avatar={avatar_uuid} template_set={int(bool(PROFILE_IMAGE_URL_TEMPLATE))} "
            f"template_uses_username={int(template_uses_username)}"
        )
        log_line(
            f"profile_image_flow avatar={avatar_uuid} template_uses_username={int(template_uses_username)}"
        )
        log_line(
            "profile_image_identity "
            f"avatar={avatar_uuid} image_uuid={image_uuid or 'none'} username={username or 'none'}"
        )
        if image_uuid or template_uses_username:
            image_bytes, content_type, image_url = download_profile_image(
                image_uuid, username=username
            )
            image_vibe_tags = vibe_tags_from_image_bytes(image_bytes)
            image_analyzed = bool(image_bytes)
            log_line(
                f"image_vibe_tags avatar={avatar_uuid} tags={len(image_vibe_tags)} analyzed={int(image_analyzed)}"
            )
            if image_bytes:
                extension = "bin"
                if content_type and "/" in content_type:
                    extension = content_type.split("/", 1)[-1].split(";")[0].strip()
                image_path = profile_image_path(avatar_uuid, extension)
                ensure_dir(image_path.parent)
                image_path.write_bytes(image_bytes)
                log_line(f"profile_image_saved avatar={avatar_uuid} path={image_path.name}")
            else:
                log_line(f"profile_image_missing_bytes avatar={avatar_uuid}")
        else:
            log_line(
                f"profile_image_missing_inputs avatar={avatar_uuid} template_uses_username={int(template_uses_username)}"
            )
    else:
        log_line(
            "profile_image_disabled "
            f"avatar={avatar_uuid} template_set={int(bool(PROFILE_IMAGE_URL_TEMPLATE))} "
            f"env_flag={_PROFILE_IMAGE_ENABLED_ENV or 'unset'}"
        )

    if not image_url:
        image_url = build_profile_image_url(image_uuid, username)

    if PROFILE_VISION_ENABLED and image_url:
        visual_profile = analyze_profile_image(
            avatar_uuid,
            image_url=image_url,
            image_bytes=image_bytes if image_analyzed else None,
            content_type=content_type,
        )
        if visual_profile:
            style_tags = visual_profile.get("style_tags") or []
            vibe_keywords = visual_profile.get("vibe_keywords") or []
            image_vibe_tags = [
                str(item)
                for item in (style_tags + vibe_keywords)
                if isinstance(item, str) and item
            ][:8]
            log_line(
                f"profile_vision_ok avatar={avatar_uuid} style_tags={len(style_tags)}"
            )

    safe_hooks = []
    if visual_profile:
        hooks = visual_profile.get("safe_hooks")
        if isinstance(hooks, list):
            safe_hooks = [str(item) for item in hooks if item][:4]

    card = {
        "avatar_uuid": avatar_uuid,
        "display_name": display_name or "Unknown",
        "username": username or "Unknown",
        "profile_keywords": keywords,
        "image_vibe_tags": image_vibe_tags,
        "safe_personalization": build_safe_personalization(
            keywords, image_vibe_tags, safe_hooks
        ),
        "bio": {
            "about_full": about_text,
            "about_summary": about_summary,
        },
        "visual_profile": visual_profile or {},
        "visual_profile_updated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        if visual_profile
        else None,
        "source_notes": {
            "web_profiledata": bool(web_profile),
            "lsl_avatar_name_used": bool(avatar_name),
            "lsl_display_name_used": bool(avatar_display_name),
            "lsl_username_used": bool(avatar_username),
            "web_profile_used": web_profile_used,
            "image_analyzed": image_analyzed,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    log_line(f"enrich_complete avatar={avatar_uuid} keywords={len(keywords)}")
    detail_lines = [
        f"avatar_uuid: {avatar_uuid}",
        f"username: {username or 'Unknown'}",
        f"display_name: {display_name or 'Unknown'}",
        f"profile_url: {web_profile.get('url', '') if isinstance(web_profile, dict) else ''}",
        f"summary: {about_text}".strip(),
        "",
    ]
    save_profile_detail(avatar_uuid, "\n".join(detail_lines).strip() + "\n")
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
