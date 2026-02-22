# OpenAI Status Page Tracker

A lightweight Python script that monitors the [OpenAI Status Page](https://status.openai.com/) in real-time and logs incidents, outages, and service degradations to the console.

## How It Works

- Uses the **Atlassian Statuspage v2 JSON API** (`/api/v2/summary.json`, `/api/v2/incidents.json`) — no HTML scraping
- **Change detection** via in-memory diff engine — only prints when something actually changes (new incident, status update, component transition)
- **Adaptive polling** — checks every 15s during active incidents, every 60s when all systems are operational
- **HTTP Conditional Requests** (`ETag` / `If-Modified-Since`) to minimize bandwidth
- Built on `asyncio` + `aiohttp` — single-threaded, non-blocking, scales to 100+ status pages concurrently

## Setup

```bash
pip install aiohttp
```

## Usage

```bash
# Live monitoring (runs continuously, alerts on changes)
python3 openai_status_tracker.py

# Show recent incidents and component statuses
python3 openai_status_tracker.py --replay

# Live monitoring with debug output (shows each poll)
python3 openai_status_tracker.py --debug

# Custom poll interval (seconds)
python3 openai_status_tracker.py --interval 30
```

Press `Ctrl+C` to stop.

## Scalability

The architecture supports monitoring multiple status pages concurrently:

```python
monitors = [
    StatusPageMonitor("https://status.openai.com"),
    StatusPageMonitor("https://status.stripe.com"),
    StatusPageMonitor("https://status.github.com"),
]
await asyncio.gather(*[m.run() for m in monitors])
```

Each monitor runs as an independent async task with its own state — a single process can handle 100+ pages.

## Dependencies

- Python 3.8+
- `aiohttp` — async HTTP client
