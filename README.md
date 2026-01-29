# SLQuest v1 (Chat-Only Prototype)

SLQuest v1 is a minimal chat relay: an LSL script starts a session on touch and relays public chat to a Python server, which calls OpenAI and returns a short reply.

## Server setup

```bash
pip install -r requirements.txt
cp SLQuest.env.example SLQuest.env
```

Edit `SLQuest.env` with your settings:

```env
PORT=8001
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5.2
WEB_SEARCH_ENABLED=0
WEB_SEARCH_ALLOWED_DOMAINS=
SLQUEST_ADMIN_TOKEN=put_long_random_token_here
PROFILE_CARD_TTL_DAYS=7
PROFILE_ENRICHER_ENABLED=1
PROFILE_ENRICHER_URL=http://localhost:8002/profile/enrich
PROFILE_ENRICHER_TIMEOUT_SECONDS=0.6
PROFILE_IMAGE_ENABLED=0
PROFILE_IMAGE_URL_TEMPLATE=
CORRADE_PROFILE_ENDPOINT=
CORRADE_API_KEY=
CORRADE_TIMEOUT_SECONDS=4.0
```

Run the server (manual):

```bash
python SLQuest_ServerHTTP_API.py
```

Run the profile enricher service (manual, separate process):

```bash
python enrich/profile_enricher_server.py
```

## Running as Linux services (recommended for "live" use)

This repo can be run under systemd so it survives reboots and is easy to restart.

Services:
- `slquest.service` → main HTTP API (default `PORT=8001`)
- `slquest-profile-enricher.service` → profile enricher (default `:8002`)
- `cloudflared.service` → Cloudflare Tunnel (routes `https://api.slquest.net` → `http://localhost:8001`)

Cambot (Firestorm UI automation) lives in `cambot/`:
- `cambot/cambot.py` → CLI automation (wmctrl/xdotool)
- `cambot/cambot_api.py` → FastAPI wrapper (optional)

Common commands:

```bash
# Main API
sudo systemctl status slquest
sudo systemctl restart slquest
sudo journalctl -u slquest -f

# Profile enricher
sudo systemctl status slquest-profile-enricher
sudo systemctl restart slquest-profile-enricher
sudo journalctl -u slquest-profile-enricher -f

# Cloudflare tunnel
sudo systemctl status cloudflared
sudo systemctl restart cloudflared
sudo journalctl -u cloudflared -f
```

Note:
- The systemd unit files live in `/etc/systemd/system/` (local machine config).
- Keep secrets in `SLQuest.env` and **do not** commit it.
- Once you run under systemd, **do not** also start the server manually (it will collide on port 8001). Use `systemctl restart slquest` instead.

## LSL script setup

1. Open `lsl/SLQuest_ChatClient.lsl`.
2. Set `SERVER_BASE` to your server URL (including port).
3. Drop the script into an in-world object.
4. Touch the object to start a chat session, then talk in public chat near it.

Tip: You can override `SERVER_BASE` or `SERVER_URL` for all scripts in a linkset by placing
`SERVER_BASE=http://your-host:port` (or `SERVER_URL=http://your-host:port`) in the object's
description so you don't have to edit every script.

## LSL coding guidelines

- Never use `key` as a variable or parameter name in LSL scripts; `key` is a type keyword and is reserved for type declarations.

## Curl tests

```bash
curl http://localhost:8001/health
```

```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{
    "npc_id": "SLQuest_DefaultNPC",
    "avatar_key": "00000000-0000-0000-0000-000000000000",
    "avatar_name": "First Last",
    "object_key": "11111111-1111-1111-1111-111111111111",
    "object_name": "SLQuest Object",
    "region": "Test Region",
    "message": "Hello there",
    "ts": "2024-01-01T00:00:00Z"
  }'
```

## Debugging OpenAI

- Ensure `SLQuest.env` lives beside `SLQuest_ServerHTTP_API.py` (same folder).
- Check logs in `logs/`, including the per-run `logs/SLQuest_<TS>.log` and the append-only `logs/SLQuest_errors.log`.
- If you see `".responses"` errors, run `python -m pip install -U openai`.
- Minimal `/chat` check:

```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello","avatar_key":"debug-avatar","npc_id":"SLQuest_DefaultNPC"}'
```

## Profile enricher notes

- The profile enricher builds `profiles/<avatar_uuid>/profile_card.json` with a TTL (default 7 days).
- It also writes `profiles/<avatar_uuid>/profile_detail.txt` with scraped profile text for later use.
- It writes `profiles/<avatar_uuid>/profile_card.txt` with a readable summary, including any visual description from profile images.
- Profile data is scraped from public web profile pages; image downloads are saved to `profiles/<avatar_uuid>/profile_image.<ext>`.
- `PROFILE_IMAGE_ENABLED` defaults to on when `PROFILE_IMAGE_URL_TEMPLATE` is set; set it to `0` to force-disable downloads.
- `PROFILE_IMAGE_URL_TEMPLATE` supports `{image_uuid}` and `{username}` placeholders to download profile images.
- If enrichment fails, the NPC responder falls back without personalization.
- Logs for enrichment live in `logs/profile_enricher.log`.
- Refresh a profile card immediately with the admin endpoint:

```bash
curl -X POST http://localhost:8001/admin/profile/refresh \
  -H "Content-Type: application/json" \
  -d '{"admin_token":"your_token","avatar_uuid":"00000000-0000-0000-0000-000000000000"}'
```

## Notes

- `logs/` and `chat/` are runtime directories and are ignored by git.
- Do not commit `SLQuest.env` or any API keys.
- Enabling web search is optional and can increase latency/cost; leave it off unless needed.
