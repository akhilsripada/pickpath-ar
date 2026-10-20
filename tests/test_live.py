"""End-to-end: start the real server on a free port and use it over HTTP, SSE and MCP."""
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "mcp")]

import server  # noqa: E402
from pickpath.copilot import Copilot  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.floor = server.Floor(speedup=20, seed=1)
        cls.floor.start()
        port = free_port()
        cls.http = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(cls.floor, Copilot(cls.floor.wh)))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_state_advances(self):
        t1 = self.get("/api/state")["t"]
        time.sleep(0.5)
        t2 = self.get("/api/state")["t"]
        self.assertGreater(t2, t1)

    def test_sse_stream_sends_state(self):
        with urllib.request.urlopen(self.base + "/events", timeout=5) as r:
            line = r.readline().decode()
        self.assertTrue(line.startswith("data: "))
        msg = json.loads(line[6:])
        self.assertEqual(msg["type"], "state")
        self.assertEqual(len(msg["agents"]), 10)

    def test_order_route_and_ask(self):
        nav = self.get("/api/orders/4417")
        self.assertEqual(nav["order"]["item"], "USB-C hub")
        self.assertGreater(len(nav["steps"]), 1)
        out = self.post("/api/ask", {"text": "Where is order 4417?"})
        self.assertIn("aisle C, bay 12", out["text"])
        self.assertLess(out["ms"], 100)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/orders/1")
        self.assertEqual(ctx.exception.code, 404)

    def test_pages_served(self):
        for path in ("/", "/ar"):
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                self.assertIn(b"<html", r.read())

    def test_keep_alive_connection_survives_a_post_with_a_body(self):
        import http.client
        c = http.client.HTTPConnection(self.base.split("//")[1], timeout=5)
        c.request("POST", "/api/reroute", body=b"{}", headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.request("POST", "/api/ask", body=json.dumps({"text": "order status"}), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("orders this wave", json.loads(r.read())["text"])

    def test_mcp(self):
        import pickpath_mcp as m
        m.BASE = self.base
        self.assertEqual(m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
                         ["serverInfo"]["name"], "pickpath")
        names = {t["name"] for t in m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(names, {"ask_pickpath", "find_order", "floor_status", "reroute_pickers"})
        out = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "find_order", "arguments": {"order_id": "4417"}}})["result"]
        text = out["content"][0]["text"]
        self.assertIn("aisle C, bay 12", text)
        self.assertIn("/ar?order=4417", text)
        out = m.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "find_order", "arguments": {"order_id": "1"}}})["result"]
        self.assertIn("no order 1", out["content"][0]["text"])
        status = m.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"name": "floor_status", "arguments": {}}})["result"]["content"][0]["text"]
        self.assertIn("People per aisle", status)
        self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


class McpOffline(unittest.TestCase):
    def test_unreachable(self):
        import pickpath_mcp as m
        old, m.BASE = m.BASE, f"http://127.0.0.1:{free_port()}"
        try:
            out = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "floor_status", "arguments": {}}})["result"]
        finally:
            m.BASE = old
        self.assertTrue(out["isError"])
        self.assertIn("python run.py", out["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
