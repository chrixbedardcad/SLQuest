#!/usr/bin/env python3
"""cambot_api.py

HTTP API wrapper around cambot.py to trigger Firestorm UI automation remotely.

Runs on the same machine as Firestorm.

Security:
  Uses a shared bearer token.
  Set env var CAMBOT_TOKEN before starting.

Start (dev):
  cd /home/chrix/clawd/cambot
  CAMBOT_TOKEN='change-me' ./.venv/bin/uvicorn cambot_api:app --host 0.0.0.0 --port 8788

Endpoints:
  GET  /health
  POST /snap
  POST /seq   {"waypoints":[1,2,3], "delay": 2.0}

"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import cambot


def require_auth(authorization: Optional[str]) -> None:
    """Authorize request.

    Insecure mode (for LAN testing):
      - If CAMBOT_TOKEN is unset/empty, authentication is bypassed.

    Secure mode:
      - If CAMBOT_TOKEN is set, require `Authorization: Bearer <token>`.
    """

    token = os.environ.get("CAMBOT_TOKEN")

    # Insecure mode: bypass auth if token not configured
    if not token:
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if provided != token:
        raise HTTPException(status_code=403, detail="Invalid token")


app = FastAPI(title="Cambot API", version="0.1")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    # Tiny GUI for quick manual testing.
    return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Cambot Control</title>
  <style>
    body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;max-width:820px}
    button{padding:12px 16px;margin:6px 6px 6px 0;font-size:16px;cursor:pointer}
    input{padding:10px;font-size:16px;width:120px;margin-right:8px}
    pre{background:#111;color:#0f0;padding:12px;overflow:auto}
    .row{margin:10px 0}
  </style>
</head>
<body>
  <h2>Cambot Control (LAN test UI)</h2>
  <div class=\"row\">
    <button onclick=\"callHealth()\">Health</button>
    <button onclick=\"callSnap()\">Take Snapshot</button>
  </div>

  <div class=\"row\">
    <strong>Waypoint Sequence</strong><br/>
    <label>Delay (sec): <input id=\"delay\" type=\"number\" step=\"0.1\" value=\"2\" /></label>
    <button onclick=\"callSeq([1,2,3])\">Run 1→2→3</button>
    <button onclick=\"callSeq([1])\">Run 1</button>
    <button onclick=\"callSeq([2])\">Run 2</button>
    <button onclick=\"callSeq([3])\">Run 3</button>
  </div>

  <div class=\"row\">
    <pre id=\"out\">Ready.</pre>
  </div>

<script>
  async function req(path, opts={}){
    const out=document.getElementById('out');
    out.textContent = 'Working...';
    try{
      const res = await fetch(path, opts);
      const txt = await res.text();
      out.textContent = txt;
    } catch (e){
      out.textContent = 'ERROR: ' + e;
    }
  }
  function callHealth(){ return req('/health'); }
  function callSnap(){ return req('/snap', {method:'POST'}); }
  function callSeq(wps){
    const delay = parseFloat(document.getElementById('delay').value || '2');
    return req('/seq', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({waypoints:wps, delay})});
  }
</script>
</body>
</html>"""


class SeqReq(BaseModel):
    waypoints: List[int] = Field(..., min_length=1)
    delay: float = Field(2.0, ge=0.0, le=60.0)


@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    h = cambot.healthcheck()
    return {
        "ready": h.ready,
        "firestorm_window": h.firestorm_window,
        "snap_dir": cambot.SNAP_DIR,
        "notes": h.notes,
    }


@app.post("/snap")
def snap(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    cambot.require_tools()
    with cambot.Lock(cambot.LOCK_PATH):
        cambot.focus_firestorm()
        path = cambot.snap_to_disk()
    return {"snapshot": path}


@app.post("/seq")
def seq(req: SeqReq, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    shots = cambot.sequence(req.waypoints, delay_s=req.delay)
    return {"snapshots": shots}
