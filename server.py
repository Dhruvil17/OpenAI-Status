#!/usr/bin/env python3

import asyncio
import json
from datetime import datetime
from aiohttp import web
import aiohttp

SUMMARY_URL = "/api/v2/summary.json"
INCIDENTS_URL = "/api/v2/incidents.json"
BASE_URL = "https://status.openai.com"

IMPACT_ICONS = {"none": "ℹ️", "minor": "🟡", "major": "🟠", "critical": "🔴"}
STATUS_ICONS = {"investigating": "🔍", "identified": "🎯", "monitoring": "👀", "resolved": "✅", "postmortem": "📝"}
COMPONENT_DISPLAY = {
    "operational": "🟢 Operational",
    "degraded_performance": "🟡 Degraded Performance",
    "partial_outage": "🟠 Partial Outage",
    "major_outage": "🔴 Major Outage",
    "under_maintenance": "🔧 Under Maintenance",
}

log_lines = []
clients = []

known_incidents = {}
component_statuses = {}
first_run = True


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_time(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def push_log(line):
    log_lines.append(line)
    if len(log_lines) > 500:
        log_lines.pop(0)
    for q in clients:
        q.put_nowait(line)


async def fetch_json(session, path):
    try:
        async with session.get(f"{BASE_URL}{path}", timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        push_log(f"[{now()}] ❌ Error: {e}")
    return None


async def poll_loop():
    global first_run
    async with aiohttp.ClientSession() as session:
        while True:
            summary = await fetch_json(session, SUMMARY_URL)
            incidents_data = await fetch_json(session, INCIDENTS_URL)

            if summary and first_run:
                for comp in summary.get("components", []):
                    component_statuses[comp["id"]] = comp["status"]
                push_log(f"[{now()}] ✅ Loaded {len(component_statuses)} components")

            if summary and not first_run:
                for comp in summary.get("components", []):
                    old = component_statuses.get(comp["id"])
                    if old and old != comp["status"]:
                        old_display = COMPONENT_DISPLAY.get(old, old)
                        new_display = COMPONENT_DISPLAY.get(comp["status"], comp["status"])
                        push_log(f"[{now()}] ⚡ COMPONENT CHANGE: {comp['name']}")
                        push_log(f"    {old_display} → {new_display}")
                    component_statuses[comp["id"]] = comp["status"]

            if incidents_data:
                for inc in incidents_data.get("incidents", []):
                    iid = inc["id"]
                    updates = inc.get("incident_updates", [])
                    update_ids = {u["id"] for u in updates}

                    if iid not in known_incidents:
                        known_incidents[iid] = {"status": inc.get("status", ""), "seen": update_ids}
                        if not first_run:
                            impact = inc.get("impact", "none")
                            push_log(f"[{now()}] {IMPACT_ICONS.get(impact, '⚪')} NEW INCIDENT: {inc['name']}")
                            push_log(f"    Impact: {impact} | Status: {STATUS_ICONS.get(inc.get('status', ''), '❓')} {inc.get('status', '')}")
                    else:
                        known = known_incidents[iid]
                        new_ids = update_ids - known["seen"]
                        if new_ids:
                            for u in sorted([u for u in updates if u["id"] in new_ids], key=lambda x: x.get("created_at", ""), reverse=True):
                                s = u.get("status", "")
                                body = u.get("body", "")
                                push_log(f"[{fmt_time(u['created_at'])}] {STATUS_ICONS.get(s, '❓')} UPDATE: {inc['name']}")
                                push_log(f"    Status: {s}{(' — ' + body) if body else ''}")
                            known["seen"] = update_ids
                            known["status"] = inc.get("status", "")

            if first_run:
                first_run = False
                active = sum(1 for v in known_incidents.values() if v["status"] not in ("resolved", "postmortem"))
                push_log(f"[{now()}] 📊 Tracking {len(known_incidents)} incidents ({active} active)")
                if active:
                    push_log(f"[{now()}] ⚠️  Active incidents detected — polling every 15s")
                else:
                    push_log(f"[{now()}] ✅ All systems operational — polling every 60s")

            has_active = any(v["status"] not in ("resolved", "postmortem") for v in known_incidents.values())
            await asyncio.sleep(15 if has_active else 60)


async def sse_handler(request):
    q = asyncio.Queue()
    clients.append(q)
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    await resp.prepare(request)

    for line in log_lines:
        await resp.write(f"data: {line}\n\n".encode())

    try:
        while True:
            line = await q.get()
            await resp.write(f"data: {line}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        clients.remove(q)
    return resp


async def index_handler(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def health_handler(request):
    return web.Response(text="ok")


async def start_background(app):
    app["poll_task"] = asyncio.create_task(poll_loop())


async def cleanup_background(app):
    app["poll_task"].cancel()


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAI Status Tracker</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 14px;
    padding: 20px;
  }
  #header {
    color: #58a6ff;
    font-weight: bold;
    padding-bottom: 12px;
    border-bottom: 1px solid #21262d;
    margin-bottom: 16px;
    font-size: 16px;
  }
  #header span { color: #8b949e; font-weight: normal; font-size: 13px; }
  #log {
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.7;
  }
  .line { padding: 2px 0; }
</style>
</head>
<body>
<div id="header">
  ⚡ OpenAI Status Tracker — Live<br>
  <span>Streaming updates from status.openai.com</span>
</div>
<div id="log"></div>
<script>
const log = document.getElementById('log');
const src = new EventSource('/stream');
src.onmessage = function(e) {
  const div = document.createElement('div');
  div.className = 'line';
  div.textContent = e.data;
  log.appendChild(div);
  window.scrollTo(0, document.body.scrollHeight);
};
src.onerror = function() {
  const div = document.createElement('div');
  div.className = 'line';
  div.textContent = '[' + new Date().toLocaleString() + '] ⚠️ Connection lost — reconnecting...';
  div.style.color = '#f85149';
  log.appendChild(div);
};
</script>
</body>
</html>"""


import os
PORT = int(os.environ.get("PORT", 8080))

app = web.Application()
app.router.add_get("/", index_handler)
app.router.add_get("/stream", sse_handler)
app.router.add_get("/health", health_handler)
app.on_startup.append(start_background)
app.on_cleanup.append(cleanup_background)

if __name__ == "__main__":
    print(f"Starting server on http://localhost:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)
