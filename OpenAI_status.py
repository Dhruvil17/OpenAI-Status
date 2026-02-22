#!/usr/bin/env python3

import asyncio
import argparse
import signal
import sys
from datetime import datetime, timezone
from typing import Optional
import aiohttp

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"

IMPACT_ICONS = {"none": "ℹ️ ", "minor": "🟡", "major": "🟠", "critical": "🔴"}
STATUS_ICONS = {"investigating": "🔍", "identified": "🎯", "monitoring": "👀", "resolved": "✅", "postmortem": "📝"}
COMPONENT_DISPLAY = {
    "operational": ("🟢", GREEN, "Operational"),
    "degraded_performance": ("🟡", YELLOW, "Degraded Performance"),
    "partial_outage": ("🟠", YELLOW, "Partial Outage"),
    "major_outage": ("🔴", RED, "Major Outage"),
    "under_maintenance": ("🔧", CYAN, "Under Maintenance"),
}


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_time(ts):
    return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StatusPageMonitor:
    SUMMARY_URL = "/api/v2/summary.json"
    INCIDENTS_URL = "/api/v2/incidents.json"
    CALM_INTERVAL = 60
    ACTIVE_INTERVAL = 15

    def __init__(self, base_url="https://status.openai.com", interval=None, debug=False):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.adaptive = interval is None
        self.debug = debug

        self.summary_etag = None
        self.summary_modified = None
        self.incidents_etag = None
        self.incidents_modified = None

        self.known_incidents = {}
        self.component_statuses = {}
        self.component_names = {}

        self.first_run = True
        self.running = True

    def stop(self):
        self.running = False

    def get_interval(self):
        if self.interval is not None:
            return self.interval
        has_active = any(v.get("status") not in ("resolved", "postmortem") for v in self.known_incidents.values())
        return self.ACTIVE_INTERVAL if has_active else self.CALM_INTERVAL

    async def fetch(self, session, url, etag, modified):
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                new_etag = resp.headers.get("ETag") or etag
                new_modified = resp.headers.get("Last-Modified") or modified

                if resp.status == 304:
                    return 304, None, etag, modified
                if resp.status == 200:
                    return 200, await resp.json(), new_etag, new_modified

                print(f"\n{RED}  ❌ HTTP {resp.status} from {url}{RESET}")
                return resp.status, None, etag, modified
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            print(f"\n{RED}  ❌ {e}{RESET}")
            return 0, None, etag, modified

    def process_summary(self, data):
        changes = False
        for comp in data.get("components", []):
            cid, name, status = comp["id"], comp["name"], comp["status"]
            self.component_names[cid] = name
            old = self.component_statuses.get(cid)

            if old is not None and old != status:
                old_info = COMPONENT_DISPLAY.get(old, ("⚪", "", old))
                new_info = COMPONENT_DISPLAY.get(status, ("⚪", "", status))
                print(f"\n{BLUE}{BOLD}  ⚡ COMPONENT STATUS CHANGE{RESET}")
                print(f"{DIM}  [{now()}]{RESET}")
                print(f"  {BOLD}Service:{RESET} {name}")
                print(f"  {BOLD}Change:{RESET}  {old_info[0]} {old_info[2]} → {new_info[0]} {new_info[2]}")
                print(f"{DIM}{'─' * 64}{RESET}")
                changes = True

            self.component_statuses[cid] = status
        return changes

    def process_incidents(self, data, suppress=False):
        changes = False

        for inc in data.get("incidents", []):
            iid = inc["id"]
            updates = inc.get("incident_updates", [])
            update_ids = {u["id"] for u in updates}

            if iid not in self.known_incidents:
                self.known_incidents[iid] = {
                    "updated_at": inc.get("updated_at", ""),
                    "status": inc.get("status", ""),
                    "seen_update_ids": update_ids,
                }
                if not suppress:
                    impact = inc.get("impact", "none")
                    status = inc.get("status", "unknown")
                    body = updates[0].get("body", "") if updates else ""
                    print(f"\n{RED}{BOLD}  {IMPACT_ICONS.get(impact, '⚪')} NEW INCIDENT{RESET}")
                    print(f"{DIM}  [{now()}]{RESET}")
                    print(f"  {BOLD}Name:{RESET}    {inc['name']}")
                    print(f"  {BOLD}Impact:{RESET}  {impact}")
                    print(f"  {BOLD}Status:{RESET}  {STATUS_ICONS.get(status, '❓')} {status}")
                    if body:
                        print(f"  {BOLD}Message:{RESET} {body}")
                    print(f"{DIM}{'─' * 64}{RESET}")
                    changes = True
            else:
                known = self.known_incidents[iid]
                new_ids = update_ids - known["seen_update_ids"]
                if new_ids:
                    new_updates = sorted(
                        [u for u in updates if u["id"] in new_ids],
                        key=lambda u: u.get("created_at", ""), reverse=True,
                    )
                    for upd in new_updates:
                        s = upd.get("status", "unknown")
                        body = upd.get("body", "")
                        color = GREEN if s == "resolved" else YELLOW if s == "monitoring" else MAGENTA
                        print(f"\n{color}{BOLD}  {STATUS_ICONS.get(s, '❓')} INCIDENT UPDATE{RESET}")
                        print(f"{DIM}  [{fmt_time(upd.get('created_at', ''))}]{RESET}")
                        print(f"  {BOLD}Name:{RESET}    {inc['name']}")
                        print(f"  {BOLD}Status:{RESET}  {STATUS_ICONS.get(s, '❓')} {s}")
                        if body:
                            print(f"  {BOLD}Message:{RESET} {body}")
                        print(f"{DIM}{'─' * 64}{RESET}")
                        changes = True

                    known["seen_update_ids"] = update_ids
                    known["updated_at"] = inc.get("updated_at", "")
                    known["status"] = inc.get("status", "")

        return changes

    async def poll(self, session):
        changes = False

        status, data, self.summary_etag, self.summary_modified = await self.fetch(
            session, f"{self.base_url}{self.SUMMARY_URL}", self.summary_etag, self.summary_modified
        )
        if status == 200 and data:
            changes |= self.process_summary(data)

        status, data, self.incidents_etag, self.incidents_modified = await self.fetch(
            session, f"{self.base_url}{self.INCIDENTS_URL}", self.incidents_etag, self.incidents_modified
        )
        if status == 200 and data:
            changes |= self.process_incidents(data, suppress=self.first_run)

        active = sum(1 for v in self.known_incidents.values() if v.get("status") not in ("resolved", "postmortem"))

        if self.debug:
            tag = "changes detected" if changes else "no new changes"
            print(f"{DIM}  [{now()}] Poll: {status} — {tag} | Active: {active}{RESET}")

        if self.first_run:
            self.first_run = False
            print(f"{DIM}  ℹ️  Loaded: {len(self.component_statuses)} components, {len(self.known_incidents)} incidents{RESET}")
            if active:
                print(f"{DIM}  ⚠️  {active} active incident(s) — polling every {self.ACTIVE_INTERVAL}s{RESET}")
            else:
                print(f"{DIM}  ✅ All systems operational — polling every {self.get_interval()}s{RESET}")
            print()

        return changes

    async def run(self):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5, keepalive_timeout=60)) as session:
            while self.running:
                try:
                    await self.poll(session)
                except Exception as e:
                    print(f"\n{RED}  ❌ {e}{RESET}")
                try:
                    await asyncio.sleep(self.get_interval())
                except asyncio.CancelledError:
                    break

    async def replay(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}{self.SUMMARY_URL}", timeout=aiohttp.ClientTimeout(total=30)) as r:
                summary = await r.json() if r.status == 200 else {}
            async with session.get(f"{self.base_url}{self.INCIDENTS_URL}", timeout=aiohttp.ClientTimeout(total=30)) as r:
                incidents_data = await r.json() if r.status == 200 else {}

        components = summary.get("components", [])
        overall = summary.get("status", {}).get("description", "Unknown")

        print(f"\n{CYAN}{BOLD}{'═' * 64}{RESET}")
        print(f"{CYAN}{BOLD}  ⚡ OpenAI Status — Replay Mode{RESET}")
        print(f"{DIM}  Overall: {overall}{RESET}")
        print(f"{CYAN}{BOLD}{'═' * 64}{RESET}\n")

        print(f"{BOLD}  📋 Components ({len(components)}):{RESET}")
        print(f"{DIM}  {'─' * 56}{RESET}")
        for c in components:
            info = COMPONENT_DISPLAY.get(c["status"], ("⚪", "", c["status"]))
            print(f"    {info[0]} {info[1]}{c['name']:<30}{RESET} {info[2]}")

        incidents = incidents_data.get("incidents", [])
        print(f"\n{DIM}{'─' * 64}{RESET}")
        print(f"\n{BOLD}  📜 Recent Incidents ({len(incidents)}):{RESET}")
        print(f"{DIM}  {'─' * 56}{RESET}")

        for inc in incidents[:10]:
            impact = inc.get("impact", "none")
            status = inc.get("status", "unknown")
            resolved = fmt_time(inc["resolved_at"]) if inc.get("resolved_at") else "ongoing"

            print(f"\n  {IMPACT_ICONS.get(impact, '⚪')} {BOLD}{inc['name']}{RESET}")
            print(f"     Status: {STATUS_ICONS.get(status, '❓')} {status} | Impact: {impact}")
            print(f"     Created: {fmt_time(inc['created_at'])}")
            if inc.get("resolved_at"):
                print(f"     Resolved: {resolved}")

            for u in reversed(inc.get("incident_updates", [])):
                body = f" — {u['body']}" if u.get("body") else ""
                print(f"       {STATUS_ICONS.get(u.get('status', ''), '·')} {fmt_time(u['created_at'])} → {u.get('status', '?')}{body}")

        print(f"\n{DIM}{'─' * 64}{RESET}")
        print(f"\n{DIM}  Done. Run without --replay for live monitoring.{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="OpenAI Status Page Tracker")
    parser.add_argument("--replay", action="store_true", help="Show recent incidents and exit")
    parser.add_argument("--interval", type=int, default=None, help="Poll interval in seconds")
    parser.add_argument("--url", type=str, default="https://status.openai.com", help="Status page URL")
    parser.add_argument("--debug", action="store_true", help="Show poll debug output")
    args = parser.parse_args()

    monitor = StatusPageMonitor(base_url=args.url, interval=args.interval, debug=args.debug)

    def shutdown(sig, frame):
        print(f"\n{DIM}  Shutting down...{RESET}")
        monitor.stop()
        print(f"\n{CYAN}{BOLD}{'═' * 64}{RESET}")
        print(f"{CYAN}  Stopped at {now()}{RESET}")
        print(f"{CYAN}{BOLD}{'═' * 64}{RESET}\n")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.replay:
        asyncio.run(monitor.replay())
    else:
        print(f"\n{CYAN}{BOLD}{'═' * 64}{RESET}")
        print(f"{CYAN}{BOLD}  ⚡ OpenAI Status Tracker{RESET}")
        print(f"{DIM}  Poll: {args.interval or 60}s {'(fixed)' if args.interval else '(adaptive)'} | {args.url}{RESET}")
        print(f"{CYAN}{BOLD}{'═' * 64}{RESET}\n")
        asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
