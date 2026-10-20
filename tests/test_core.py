import heapq
import io
import json
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pickpath.copilot import Copilot, GeminiRouter, navigation, parse_intent  # noqa: E402
from pickpath.floor import AISLES, NBAY, Floor, bin_node  # noqa: E402
from pickpath.nav import steps  # noqa: E402
from pickpath.sim import HOT_AISLE, OVERFLOW_AISLE, Warehouse  # noqa: E402


def dijkstra(floor, a, b, occ):
    """Reference implementation to check A* against."""
    from pickpath.floor import AISLE_CAPACITY, CONGESTION_PENALTY
    dist, pq = {a: 0.0}, [(0.0, a)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == b:
            return d
        if d > dist[u]:
            continue
        for v, w, aisle in floor.adj[u]:
            if aisle is not None:
                w *= 1 + CONGESTION_PENALTY * max(0, occ.get(aisle, 0) - AISLE_CAPACITY)
            if d + w < dist.get(v, math.inf):
                dist[v] = d + w
                heapq.heappush(pq, (d + w, v))


class FloorTests(unittest.TestCase):
    def setUp(self):
        self.f = Floor()

    def test_graph_shape(self):
        self.assertEqual(len(self.f.xy), 8 * (NBAY + 2) + 2)
        for u, edges in self.f.adj.items():
            for v, d, _ in edges:
                self.assertIn(u, [x for x, _, _ in self.f.adj[v]], "edges are two-way")

    def test_astar_matches_dijkstra(self):
        rng = random.Random(0)
        nodes = list(self.f.xy)
        for _ in range(300):
            a, b = rng.choice(nodes), rng.choice(nodes)
            occ = {rng.randrange(8): rng.randint(0, 6) for _ in range(3)}
            _, cost = self.f.route(a, b, occ)
            self.assertAlmostEqual(cost, dijkstra(self.f, a, b, occ), places=6)

    def test_route_from_dock_to_bin(self):
        path, cost = self.f.route("DOCK", "C12")
        self.assertEqual(path[:2], ["DOCK", "J"])
        self.assertEqual(path[-1], "C12")
        self.assertAlmostEqual(cost, 5.5 + 11 + 2)

    def test_router_walks_around_a_jammed_aisle(self):
        clear, _ = self.f.route("BT", "BB")
        self.assertIn("B6", clear)                       # straight down aisle B
        jammed, _ = self.f.route("BT", "BB", {1: 9})     # 9 people in aisle B
        self.assertFalse(any(n[0] == "B" and n[1:].isdigit() for n in jammed))
        self.assertTrue(any(n[0] in "AC" and n[1:].isdigit() for n in jammed))  # goes round via A or C

    def test_polyline_keeps_only_corners(self):
        path, _ = self.f.route("DOCK", "C12")
        self.assertEqual(self.f.polyline(path), [(32.0, 37.5), (32.0, 32.0), (21.0, 32.0), (21.0, 30.0)])


class NavTests(unittest.TestCase):
    def test_turns_and_wording(self):
        s = steps([(32, 37.5), (32, 32), (21, 32), (21, 30)], aisle_letter="C", bay=12, level=3, side="right")
        self.assertEqual([x["turn"] for x in s], ["left", "right", "arrive"])
        self.assertIn("main aisle", s[0]["text"])
        self.assertIn("into aisle C", s[1]["text"])
        self.assertIn("Bay C-12 is on your right, level 3", s[2]["text"])

    def test_already_there(self):
        self.assertEqual(steps([(1, 1)], aisle_letter="A", bay=1, level=1, side="left")[0]["turn"], "arrive")


class SimTests(unittest.TestCase):
    def test_deterministic(self):
        a, b = Warehouse(seed=5), Warehouse(seed=5)
        a.run(300); b.run(300)
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_orders_flow_through_every_state(self):
        w = Warehouse(seed=2)
        w.run(900)
        statuses = {o.status for o in w.orders.values()}
        self.assertTrue({"shipped"} <= statuses)
        for o in w.orders.values():
            if o.status == "shipped":
                self.assertGreaterEqual(o.shipped_at, o.picked_at)
                self.assertGreater(o.picked_at, o.created)

    def test_agents_stay_on_the_floor(self):
        w = Warehouse(seed=4)
        for _ in range(2000):
            w.step(0.5)
            for a in w.agents:
                self.assertTrue(0 <= a.x <= 64 and 0 <= a.y <= 42, a)

    def test_starts_congested_in_the_hot_aisle(self):
        w = Warehouse(seed=1)
        self.assertGreater(w.occupancy().get(HOT_AISLE, 0), 2)

    def test_overflow_improves_on_time_rate(self):
        rates = {}
        for overflow in (False, True):
            r = []
            for seed in range(4):
                w = Warehouse(seed=seed, overflow=overflow)
                w.run(2400)
                r.append(w.on_time_rate())
            rates[overflow] = sum(r) / len(r)
        self.assertGreater(rates[True], rates[False])
        self.assertGreater(rates[True], 0.95)

    def test_enable_overflow_redirects_pickers_heading_to_hot_aisle(self):
        w = Warehouse(seed=1)
        w.run(30)
        moved = w.enable_overflow()
        for m in moved:
            self.assertTrue(m["to"].startswith(AISLES[OVERFLOW_AISLE]))
        self.assertTrue(w.overflow_enabled)


class CopilotTests(unittest.TestCase):
    def setUp(self):
        self.w = Warehouse(seed=1)
        self.w.run(60)
        self.c = Copilot(self.w)

    def test_parse(self):
        self.assertEqual(parse_intent("Where is order 4417?"), {"intent": "locate", "order": "4417"})
        self.assertEqual(parse_intent("why are orders late"), {"intent": "diagnose"})
        self.assertEqual(parse_intent("reroute pickers"), {"intent": "reroute"})
        self.assertEqual(parse_intent("where is robot R2"), {"intent": "agent", "kind": "R", "n": 2})
        self.assertEqual(parse_intent("find picker 3"), {"intent": "agent", "kind": "P", "n": 3})
        self.assertEqual(parse_intent("order status"), {"intent": "status"})
        self.assertEqual(parse_intent("tell me a joke"), {"intent": "unknown"})

    def test_locate_walkthrough_order(self):
        out = self.c.ask("Where is order 4417?")
        self.assertIn("USB-C hub", out["text"])
        self.assertEqual(out["nav"]["order"]["aisle"] + str(out["nav"]["order"]["bay"]), "C12")
        self.assertEqual(out["nav"]["path"][0], [32.0, 37.5])

    def test_diagnose_names_hot_aisle_then_reroute(self):
        out = self.c.ask("why are orders late?")
        self.assertEqual(out["text"], "Aisle F is the bottleneck.")
        self.assertIn("reroute pickers", out["next_step"])
        self.assertIn("Overflow routing is on", self.c.ask("reroute pickers")["text"])
        self.assertIn("already on", self.c.ask("reroute pickers")["text"])

    def test_unknown_order_and_agent(self):
        self.assertIn("no order 9999", self.c.ask("where is order 9999")["text"])
        self.assertIn("no robot R9", self.c.ask("where is robot 9")["text"])

    def test_navigation_helper(self):
        self.assertIsNone(navigation(self.w, "1"))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RouterTests(unittest.TestCase):
    def test_validated_and_cached(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [
                {"text": '{"intent":"locate","order":"4413"}'}]}}]}).encode())
        w = Warehouse(seed=1)
        c = Copilot(w, GeminiRouter("k", opener=opener))
        self.assertEqual(c.ask("hmm, the yoga mat thing, can you take me there")["routed_by"], "llm")
        c.ask("hmm, the yoga mat thing, can you take me there")
        self.assertEqual(len(calls), 1)

    def test_bad_output_rejected(self):
        def opener(req, timeout):
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [
                {"text": '{"intent":"locate","order":"DROP TABLE"}'}]}}]}).encode())
        self.assertEqual(GeminiRouter("k", opener=opener).route("x"), {"intent": "unknown"})


if __name__ == "__main__":
    unittest.main()
