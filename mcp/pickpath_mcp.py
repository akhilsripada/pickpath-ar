"""PickPath MCP server: lets an AI assistant (Claude Desktop, Claude Code, ChatGPT, Cursor...)
run the warehouse floor in plain English.

Model Context Protocol over stdio (JSON-RPC 2.0, one message per line), standard
library only. Every tool call goes to the PickPath HTTP API, so the assistant
sees exactly what the dashboard shows.

    python mcp/pickpath_mcp.py                      # normally started by your AI app
    PICKPATH_URL=http://my-server:8000 python mcp/pickpath_mcp.py

Claude Desktop (claude_desktop_config.json):
    {"mcpServers": {"pickpath": {"command": "python3", "args": ["/path/to/pickpath-ar/mcp/pickpath_mcp.py"]}}}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SERVER = {"name": "pickpath", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2025-06-18"
BASE = os.environ.get("PICKPATH_URL", "http://127.0.0.1:8000").rstrip("/")

TOOLS = [
    {"name": "ask_pickpath",
     "description": "Ask the live warehouse floor a plain-English question, e.g. 'where is order 4417?', "
                    "'why are orders late?', 'where is picker P3?'. Returns the answer and a dashboard link.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "find_order",
     "description": "Bin location and turn-by-turn walking directions from the packing desk to an order's item.",
     "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string", "description": "e.g. 4417"}},
                     "required": ["order_id"]}},
    {"name": "floor_status",
     "description": "Order counts by status, and people versus capacity in each aisle.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "reroute_pickers",
     "description": "Turn on overflow routing so pickers fetch best sellers from whichever copy is faster to reach.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def http(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def call_tool(name: str, args: dict) -> str:
    link = f"\n\nLive dashboard: {BASE}/"
    if name == "ask_pickpath":
        q = str(args.get("question", "")).strip()
        if not q:
            raise ValueError("question is required")
        out = http("POST", "/api/ask", {"text": q})
        lines = [out["text"]] + [f"- {r}" for r in out.get("reasons", [])]
        if out.get("next_step"):
            lines.append(out["next_step"])
        return "\n".join(lines) + link
    if name == "find_order":
        oid = str(args.get("order_id", "")).strip()
        try:
            nav = http("GET", f"/api/orders/{oid}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return f"There's no order {oid} on this wave." + link
            raise
        o = nav["order"]
        steps = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(nav["steps"]))
        return (f"Order {o['id']} ({o['item']}, {o['status']}): aisle {o['aisle']}, bay {o['bay']}, level {o['level']}, "
                f"{o['side']} side. {nav['length_m']} m from packing.\n{steps}\n\nAR directions: {BASE}/ar?order={o['id']}")
    if name == "floor_status":
        d = http("GET", "/api/diagnose")
        c = d["counts"]
        busy = [f"{a['aisle']}: {a['people']}/{a['capacity']}" for a in d["aisles"] if a["people"]]
        return (f"Orders: {c['queued']} queued, {c['picking']} picking, {c['packing']} packing, {c['shipped']} shipped, "
                f"{c['late']} late.\nPeople per aisle (people/capacity): {', '.join(busy) or 'all aisles empty'}." + link)
    if name == "reroute_pickers":
        return http("POST", "/api/reroute", {})["text"] + link
    raise ValueError(f"unknown tool {name!r}")


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                  "instructions": "Use these tools to answer questions about the live warehouse floor."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = {"content": [{"type": "text", "text": call_tool(params.get("name"), params.get("arguments") or {})}]}
        except urllib.error.URLError:
            result = {"content": [{"type": "text", "text": f"PickPath isn't reachable at {BASE}. Start it with "
                                                           "`python run.py` and try again."}], "isError": True}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = handle(json.loads(line))
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
