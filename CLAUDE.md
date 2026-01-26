# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SLQuest is a chat relay system that connects Second Life (SL) NPCs to OpenAI for AI-powered conversations. An LSL script in-world starts a session on touch, relays public chat to a Python HTTP server, which calls OpenAI and returns short replies suitable for SL chat.

## Commands

### Run the main server
```bash
python SLQuest_ServerHTTP_API.py
```

### Run the profile enricher service (separate process, port 8002)
```bash
python enrich/profile_enricher_server.py
```

### Install dependencies
```bash
pip install -r requirements.txt
```

### Health check
```bash
curl http://localhost:8001/health
```

### Test chat endpoint
```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello","avatar_key":"debug-avatar","npc_id":"SLQuest_DefaultNPC"}'
```

## Architecture

### Server Components (Python)

- **SLQuest_ServerHTTP_API.py**: Main Flask/Waitress HTTP server handling `/chat`, `/chat_async`, callback registration, admin endpoints, and quest events. Uses OpenAI Responses API with conversation threads for persistent chat context per avatar+NPC pair.

- **SLQuest_QuestEngine.py**: Quest state machine. Tracks per-player quest progress (started → clicked → completed). Injects quest context into LLM prompts and handles post-chat reward logic.

- **enrich/profile_enricher.py**: Scrapes Second Life web profiles, downloads profile images, uses OpenAI vision to extract avatar styling/appearance info for personalization.

- **enrich/profile_enricher_server.py**: Flask microservice wrapping the enricher with thread pool for async processing.

### LSL Scripts (Second Life in-world)

- **SLQuest_ChatClient.lsl**: Main client script. Handles touch to start/end sessions, listens on public chat, sends messages to server via HTTP, processes async callback responses.

- **SLQuest_CallbackReceiver.lsl**: Manages callback URL registration with server. Receives async responses and routes them to ChatClient via link messages.

- **SLQuest_ActionRouter.lsl**: Processes action commands from server responses (Give items, play sounds, animations, particles).

- **SLQuest_QuestObject.lsl**: Generic quest object script. Reads config from `quest_config` notecard, registers with shared pool, sends `object_found` events on touch.

### Data Flow

1. Player touches NPC object → LSL starts session
2. Player speaks in public chat → LSL captures and sends to `/chat_async`
3. Server builds prompt with NPC system prompt + profile personalization + quest context
4. OpenAI responds → server packages reply with any actions
5. Callback POSTs to LSL → NPC speaks reply, executes actions

### Key Directories

- **npcs/**: NPC configurations. Each NPC has `system.md` (personality prompt), `config.json` (model, display name), optionally `first_conversation.md`
- **npcs/_base/**: Base system prompt applied to all NPCs
- **pools/**: Shared quest object pool (`objects.json`) - objects self-register here
- **quests/player/**: Per-player quest state files (current quest, history)
- **profiles/**: Cached avatar profile cards and images
- **chat/**: Conversation history per avatar/NPC pair
- **logs/**: Server logs and OpenAI request traces

## Configuration

Copy `SLQuest.env.example` to `SLQuest.env` and configure:
- `OPENAI_API_KEY`: Required
- `OPENAI_MODEL`: Default `gpt-5.2`
- `PORT`: Server port (default 8001)
- `PROFILE_ENRICHER_ENABLED`: Enable avatar profile enrichment
- `PROFILE_VISION_ENABLED`: Use vision model for profile image analysis

## LSL Guidelines

### Reserved Words
LSL has type keywords that cannot be used as variable names:
- `key` - UUID type (use `obj_key`, `avatar_uuid`, `query_id` instead)
- `string`, `integer`, `float`, `vector`, `rotation`, `list` - all reserved

Example of what NOT to do:
```lsl
string key = "abc";     // ERROR: key is reserved
integer string = 5;     // ERROR: string is reserved
```

Correct alternatives:
```lsl
string obj_key = "abc";
string param_name = "abc";
integer str_len = 5;
```

### Other Guidelines
- LSL scripts read `SERVER_BASE` from object description for easy configuration
- Callback system uses link messages (LM_CB_TOKEN=9100, LM_CB_REPLY=9101, LM_ACTION=9200)
- Quest objects use `quest_config` notecard for configuration (object_id, difficulty, hint, etc.)
