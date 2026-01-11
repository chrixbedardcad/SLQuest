# SL Quest Gateway Server

A minimal Flask server that accepts requests from Second Life LSL and returns fast JSON replies.

## Requirements

- Python 3.9+

## Setup (Windows)

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Set a shared token used by the LSL script:

```bash
set SL_TOKEN=CHANGE_ME
```

Run the server (binds to 0.0.0.0 and PORT env var, default 8000):

```bash
python app.py
```

## Test with curl

Health check:

```bash
curl http://localhost:8000/health
```

POST request (include token as query param):

```bash
curl -X POST "http://localhost:8000/sl?token=CHANGE_ME" \
  -H "Content-Type: application/json" \
  -d '{
    "avatar_name": "First Last",
    "avatar_key": "00000000-0000-0000-0000-000000000000",
    "object_key": "11111111-1111-1111-1111-111111111111",
    "region": "Test Region",
    "message": "start",
    "session_id": "demo-session"
  }'
```

## Notes

- Router port-forwarding should map external port 80 to internal port 8000.
- The LSL script must call `http://slquest.duckdns.org/sl` without `:8000`.
- Keep replies fast to avoid Second Life timeouts.
