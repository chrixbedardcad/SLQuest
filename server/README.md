# SLQuest: Second Life Quest System

## What this repo is
This project is a Second Life quest system. In-world objects or animesh use an LSL script to send HTTP requests (llHTTPRequest) to a Python server. The Python server returns short responses and basic quest state. Later, the Python server can optionally call an LLM (OpenAI/Gemini/etc.) to generate dialog and quest logic. The initial version does **not** require an LLM and runs with a built-in toy quest for immediate testing.

## Naming Convention
All scripts and main entrypoints start with `SLQuest_`.

Examples:
- `SLQuest_ServerHTTP_API.py`
- `SLQuest_QuestEngine.py`
- `SLQuest_Client.lsl`

## Quick start (Windows)
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

1. Copy `SLQuest.env.example` to `SLQuest.env` (local only).
2. Edit `PORT=8001` in `SLQuest.env`.

```bash
copy SLQuest.env.example SLQuest.env
```

3. Run the server:
```bash
python SLQuest_ServerHTTP_API.py
```

## Test locally with curl
```bash
curl http://localhost:8000/health
```

```bash
curl -X POST http://localhost:8000/slquest \
  -H "Content-Type: application/json" \
  -d '{
    "avatar_name": "First Last",
    "avatar_key": "00000000-0000-0000-0000-000000000000",
    "object_name": "QuestObject",
    "object_key": "11111111-1111-1111-1111-111111111111",
    "region": "Test Region",
    "message": "start",
    "session_id": "demo-session"
  }'
```

## Deployment note
Assumes router port-forwarding: external port 80 -> internal port 8000. The LSL script must call `http://slquest.duckdns.org/slquest` without specifying `:8000`.

## Security note
- Never commit `SLQuest.env`.
- If you set `SLQUEST_TOKEN` on the server, also set `TOKEN` in the LSL script.
- LSL scripts are effectively public; never embed real API keys in LSL.

## Logging
The server writes per-request logs to `/server/logs/` and creates the directory at startup if it is missing. Each POST to `/slquest` generates a JSON file under a date folder using the format:

```
/server/logs/YYYY-MM-DD/HHMMSS_mmm_<session_id_short>_<avatar_key_short>.json
```

It also appends a one-line summary to `/server/logs/SLQuest_requests.log`.

## Run logs
Each server start creates a new run log file at `/server/logs/SLQuest_YYYYMMDD_HHMMSS.log` (UTC). The run log captures START/STOP events and one line per HTTP request, including `/health` and `/slquest`, with method, path, status code, and remote IP.

## Future: LLM Integration
Set `SLQUEST_LLM_PROVIDER` (empty by default) and add a provider-specific call inside `SLQuest_QuestEngine.py` or a new module that the quest engine can call. Placeholder env vars (like `OPENAI_API_KEY`) are provided in `SLQuest.env.example`, but no LLM calls are implemented yet.
