# Cambot HTTP API

Runs on the Firestorm machine. Provides an HTTP API to trigger the cambot.

## Setup

A venv is created in `cambot/.venv` with:
- fastapi
- uvicorn

## Start (manual)

```bash
cd /home/chrix/clawd/SLQuest/cambot
export CAMBOT_TOKEN='change-me-long-random'
./.venv/bin/uvicorn cambot_api:app --host 0.0.0.0 --port 8788
```

## Test locally

```bash
TOKEN='change-me-long-random'

curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/health | jq
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/snap | jq
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"waypoints":[1,2,3],"delay":2}' \
  http://127.0.0.1:8788/seq | jq
```

## Remote use

From another machine:

```bash
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"waypoints":[1,2,3],"delay":2}' \
  http://<FIRESTORM_LAN_IP>:8788/seq
```

## Security notes

- Keep this on LAN or behind a VPN.
- Use a long random token.
- Consider binding to LAN interface only or using a firewall.
- The API triggers UI automation: it will steal focus and type.
