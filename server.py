"""PickPath AR server: runs the floor simulation and serves the dashboard, AR page and API.

    python run.py                      # easiest: starts this and opens the browser
    python server.py --port 8000 --speedup 4

The simulation advances 10 times a second on its own thread. Browsers get the
whole floor state over Server-Sent Events 5 times a second; the server is the
single source of truth, so every screen (laptop, phone in AR) agrees.

HTTP API (also used by the MCP server in mcp/):
    GET  /api/state            agents, aisle occupancy, order counts, latest orders
    GET  /api/orders/<id>      one order with its route and turn-by-turn steps
    GET  /api/diagnose         per-aisle people, pick times and late orders
    POST /api/ask              {"text": "why are orders late?"}
    POST /api/reroute          turn on overflow routing
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from pickpath.copilot import Copilot, GeminiRouter, navigation  # noqa: E402
from pickpath.sim import Warehouse  # noqa: E402

PAGES = {"/": "index.html", "/index.html": "index.html", "/ar": "ar.html", "/ar/": "ar.html"}


class Floor(threading.Thread):
    """Owns the Warehouse. Every read or write goes through `lock`."""

    def __init__(self, speedup: float = 4.0, seed: int | None = None, tick: float = 0.1):
        super().__init__(daemon=True)
        self.wh = Warehouse(seed=seed)
        self.lock = threading.Lock()
        self.speedup, self.tick = speedup, tick
        self.clients: set[queue.Queue] = set()
        self.clients_lock = threading.Lock()

    def run(self):
        last_push, last = 0.0, time.monotonic()
        while True:
            time.sleep(self.tick)
            now = time.monotonic()
            with self.lock:
                self.wh.step((now - last) * self.speedup)
                snap = self.wh.snapshot() if now - last_push >= 0.2 and self.clients else None
            last = now
            if snap:
                last_push = now
                self.publish({"type": "state", **snap})

    def publish(self, msg: dict):
        data = json.dumps(msg, separators=(",", ":"))
        with self.clients_lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with self.clients_lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.clients_lock:
            self.clients.discard(q)


def make_handler(floor: Floor, copilot: Copilot):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if path in PAGES:
                return self._send(200, (ROOT / "web" / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
            if path == "/events":
                return self._stream()
            with floor.lock:
                if path == "/api/state":
                    return self._json(200, floor.wh.snapshot())
                if path == "/api/diagnose":
                    return self._json(200, {"aisles": floor.wh.aisle_stats(), "counts": floor.wh.counts()})
                if path.startswith("/api/orders/"):
                    nav = navigation(floor.wh, path.rsplit("/", 1)[1])
                    return self._json(200, nav) if nav else self._json(404, {"error": "no such order"})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/reroute":
                with floor.lock:
                    out = copilot.ask("reroute pickers")
                return self._json(200, out)
            if path == "/api/ask":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    text = str(json.loads(self.rfile.read(min(n, 4096)) or b"{}").get("text", "")).strip()
                except (ValueError, AttributeError):
                    return self._json(400, {"error": 'send JSON like {"text": "why are orders late?"}'})
                if not text:
                    return self._json(400, {"error": "text is empty"})
                t0 = time.perf_counter()
                with floor.lock:
                    out = copilot.ask(text[:500])
                out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                return self._json(200, out)
            self._json(404, {"error": "not found"})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = floor.subscribe()
            try:
                with floor.lock:
                    first = json.dumps({"type": "state", **floor.wh.snapshot()})
                self.wfile.write(f"data: {first}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                floor.unsubscribe(q)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--speedup", type=float, default=4.0, help="simulated seconds per real second")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    floor = Floor(args.speedup, args.seed)
    router = GeminiRouter.from_env()
    copilot = Copilot(floor.wh, router)
    floor.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(floor, copilot))
    server.daemon_threads = True
    print("copilot: rules" + (" + Gemini for free-form questions" if router else
                              " only (set GEMINI_API_KEY to add free-form questions)"))
    print(f"live at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
