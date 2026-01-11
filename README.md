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
```

Run the server:

```bash
python SLQuest_ServerHTTP_API.py
```

## LSL script setup

1. Open `lsl/SLQuest_ChatClient.lsl`.
2. Set `SERVER_BASE` to your server URL (including port).
3. Drop the script into an in-world object.
4. Touch the object to start a chat session, then talk in public chat near it.

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

## Notes

- `logs/` and `chat/` are runtime directories and are ignored by git.
- Do not commit `SLQuest.env` or any API keys.
