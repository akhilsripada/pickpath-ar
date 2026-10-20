"""A live fulfillment floor: orders arrive, pickers walk routes, aisles congest.

Everything advances on a simulated clock (`speedup` × real time) so a demo
shows a full wave of orders in a few minutes. Time-dependent behaviour:

* Orders arrive as a Poisson process and are due SLA_SECONDS after they arrive.
* An idle picker takes the oldest waiting order whose route is cheapest
  for them (route cost includes current aisle congestion).
* Walking and picking slow down in an aisle that holds more people than it
  fits. That is how one popular aisle makes a whole wave late.
* Popular items in aisle F also have an overflow copy in aisle G. Overflow
  is off until an operator turns it on (`enable_overflow`, or "reroute
  pickers"); after that each order goes to whichever copy is cheaper to
  reach right now.
"""
from __future__ import annotations

import math
import random
from collections import Counter, deque
from dataclasses import dataclass, field

from .floor import AISLE_CAPACITY, AISLES, NBAY, Floor, bin_node

SLA_SECONDS = 300
PICK_SECONDS = 12.0
DROP_SECONDS = 4.0
PACK_SECONDS = 25.0
HOT_AISLE = 5          # aisle F holds the best sellers
OVERFLOW_AISLE = 6     # aisle G holds their overflow copies
HOT_WEIGHT = 2.5       # a best seller is ordered this many times as often as other items

ITEMS = ["USB-C hub", "Yoga mat", "Espresso beans", "Desk lamp", "Phone case", "Water bottle", "Running socks",
         "Board game", "Bluetooth speaker", "Notebook set", "HDMI cable", "Air fryer liners", "Dog chew toy",
         "Laptop stand", "Face wash", "Batteries (AA)", "Coffee mug", "Puzzle 1000pc", "Wireless mouse",
         "Sunscreen", "Protein bars", "Kitchen scale", "Shower curtain", "LED strip"]


@dataclass
class Sku:
    sku: str
    item: str
    aisle: int
    bay: int
    level: int
    side: str
    popularity: float
    overflow: tuple[int, int] | None = None   # (aisle, bay) of the overflow copy


@dataclass
class Order:
    id: str
    sku: Sku
    created: float
    due: float
    status: str = "queued"          # queued -> picking -> packing -> shipped
    picker: str | None = None
    slot: str | None = None         # bin node actually used
    picked_at: float | None = None
    shipped_at: float | None = None


@dataclass
class Agent:
    id: str
    kind: str                       # "P" picker, "R" robot
    node: str
    x: float = 0.0
    y: float = 0.0
    path: list[str] = field(default_factory=list)
    leg: int = 0                    # index of the node we're walking towards
    state: str = "idle"             # idle | to_bin | picking | to_dock | dropping | moving
    order: Order | None = None
    wait: float = 0.0
    speed: float = 1.3


def skus_by_item(skus: list[Sku], item: str) -> Sku:
    return next(s for s in skus if s.item == item)


class Warehouse:
    def __init__(self, seed: int | None = 7, pickers: int = 7, robots: int = 3, order_rate: float = 1 / 12,
                 overflow: bool = False, start: float = 0.0):
        self.rng = random.Random(seed)
        self.floor = Floor()
        self.now = start
        self.order_rate = order_rate          # orders per simulated second
        self.overflow_enabled = overflow
        self.skus = self._catalog()
        self.weights = [s.popularity for s in self.skus]
        self.orders: dict[str, Order] = {}
        self.queue: deque[Order] = deque()
        self.next_order = 4410
        self.pick_log: deque[tuple[float, int, float]] = deque(maxlen=400)   # (time, aisle, seconds)
        self.events: deque[dict] = deque(maxlen=50)
        self.agents: list[Agent] = []
        for i in range(pickers):
            self.agents.append(self._place(Agent(f"P{i + 1}", "P", "DOCK", speed=1.3)))
        for i in range(robots):
            self.agents.append(self._place(Agent(f"R{i + 1}", "R", f"{AISLES[(i * 3) % 8]}B", speed=1.8)))
        # start the wave with a backlog and pickers already busy in the hot aisle
        for _ in range(12):
            self._new_order(hot=True)
        self.orders["4417"].sku = skus_by_item(self.skus, "USB-C hub")   # the walkthrough order
        for a, b in zip(self.agents[:4], (2, 5, 8, 11)):
            a.node = bin_node(HOT_AISLE, b)
            self._place(a)

    # ------------------------------------------------------------------ setup
    def _catalog(self) -> list[Sku]:
        """24 SKUs. The first 8 after the walkthrough item are best sellers slotted in aisle F,
        each with an overflow copy at the same bay in aisle G."""
        skus = [Sku("SKU-1000", "USB-C hub", 2, 12, 3, "right", 1.0)]   # fixed spot for the walkthrough: C-12
        used = {(2, 12)}
        for k, item in enumerate(ITEMS[1:], start=1):
            hot = k <= 8
            aisle = HOT_AISLE if hot else self.rng.choice([a for a in range(8) if a not in (HOT_AISLE, OVERFLOW_AISLE)])
            while True:
                bay = self.rng.randint(1, NBAY)
                if (aisle, bay) not in used:
                    used.add((aisle, bay))
                    break
            skus.append(Sku(f"SKU-{1000 + k}", item, aisle, bay, self.rng.randint(1, 4),
                            self.rng.choice(["left", "right"]), HOT_WEIGHT if hot else 1.0,
                            overflow=(OVERFLOW_AISLE, bay) if hot else None))
        return skus

    def _place(self, a: Agent) -> Agent:
        a.x, a.y = self.floor.xy[a.node]
        return a

    def _new_order(self, hot: bool = False):
        sku = self.rng.choice([s for s in self.skus if s.aisle == HOT_AISLE]) if hot else \
            self.rng.choices(self.skus, self.weights)[0]
        o = Order(str(self.next_order), sku, self.now, self.now + SLA_SECONDS)
        self.next_order += 1
        self.orders[o.id] = o
        self.queue.append(o)
        return o

    # ------------------------------------------------------------------ queries
    def occupancy(self) -> dict[int, int]:
        c = Counter()
        for a in self.agents:
            aisle = self._aisle_at(a)
            if aisle is not None:
                c[aisle] += 1
        return dict(c)

    def _aisle_at(self, a: Agent) -> int | None:
        i = round((a.x - 7) / 7)
        if 0 <= i < 8 and abs(a.x - (7 + 7 * i)) < 0.5 and 6.0 < a.y < 30.0:
            return i
        return None

    def slot_for(self, sku: Sku, start: str, occupancy: dict[int, int] | None = None) -> tuple[str, list[str], float]:
        """Pick the bin to fetch from (primary or overflow) by current route cost."""
        occ = occupancy if occupancy is not None else self.occupancy()
        options = [bin_node(sku.aisle, sku.bay)]
        if self.overflow_enabled and sku.overflow:
            options.append(bin_node(*sku.overflow))
        best = None
        for node in options:
            path, cost = self.floor.route(start, node, occ)
            aisle = self.floor.aisle_of(node)
            cost += PICK_SECONDS * self._slowdown(occ.get(aisle, 0) + 1) * 1.3   # expected wait at the shelf
            if best is None or cost < best[2]:
                best = (node, path, cost)
        return best

    @staticmethod
    def _slowdown(people: int) -> float:
        return 1.0 + 0.9 * max(0, people - AISLE_CAPACITY)

    # ------------------------------------------------------------------ simulation
    def step(self, dt: float):
        """Advance the floor by dt simulated seconds."""
        self.now += dt
        if self.rng.random() < 1 - math.exp(-self.order_rate * dt):
            self._new_order()
        occ = self.occupancy()
        for a in self.agents:
            if a.kind == "P" and a.state == "idle" and self.queue:
                self._assign(a, occ)
            self._advance(a, dt, occ)
        for o in self.orders.values():
            if o.status == "packing" and self.now - o.picked_at >= PACK_SECONDS:
                o.status, o.shipped_at = "shipped", self.now

    def _assign(self, a: Agent, occ):
        # oldest few orders first (fairness), cheapest route among them (efficiency)
        candidates = list(self.queue)[:4]
        best = min(candidates, key=lambda o: self.slot_for(o.sku, a.node, occ)[2] + (o.created - self.now) * 0.5)
        self.queue.remove(best)
        node, path, _ = self.slot_for(best.sku, a.node, occ)
        best.status, best.picker, best.slot = "picking", a.id, node
        a.order, a.state, a.path, a.leg = best, "to_bin", path, 1

    def _advance(self, a: Agent, dt: float, occ):
        if a.wait > 0:
            a.wait -= dt
            if a.wait > 0:
                return
            self._arrived_wait_done(a)
            return
        if a.state == "idle":
            if a.kind == "R":
                target = f"{AISLES[self.rng.randrange(8)]}{self.rng.choice(['T', 'B'])}"
                a.path, _ = self.floor.route(a.node, target, occ)
                a.leg, a.state = 1, "moving"
            return
        budget = a.speed * dt
        while budget > 0 and a.leg < len(a.path):
            tx, ty = self.floor.xy[a.path[a.leg]]
            aisle = self._aisle_at(a)
            slow = self._slowdown(occ.get(aisle, 0)) if aisle is not None else 1.0
            d = math.hypot(tx - a.x, ty - a.y)
            step = budget / slow
            if step >= d:
                a.x, a.y, a.node = tx, ty, a.path[a.leg]
                a.leg += 1
                budget -= d * slow
            else:
                a.x += (tx - a.x) / d * step
                a.y += (ty - a.y) / d * step
                budget = 0
        if a.leg >= len(a.path):
            self._arrived(a, occ)

    def _arrived(self, a: Agent, occ):
        if a.state == "to_bin":
            aisle = self.floor.aisle_of(a.node)
            secs = PICK_SECONDS * self._slowdown(occ.get(aisle, 0)) * self.rng.uniform(0.8, 1.25)
            a.state, a.wait = "picking", secs
            self.pick_log.append((self.now + secs, aisle, secs))
        elif a.state == "to_dock":
            a.state, a.wait = "dropping", DROP_SECONDS
        else:
            a.state, a.wait = "idle", self.rng.uniform(1, 4) if a.kind == "R" else 0

    def _arrived_wait_done(self, a: Agent):
        if a.state == "picking":
            a.path, _ = self.floor.route(a.node, "DOCK", self.occupancy())
            a.leg, a.state = 1, "to_dock"
        elif a.state == "dropping":
            a.order.status, a.order.picked_at = "packing", self.now
            a.order, a.state = None, "idle"

    def run(self, seconds: float, dt: float = 0.5):
        for _ in range(int(seconds / dt)):
            self.step(dt)

    # ------------------------------------------------------------------ operator actions
    def enable_overflow(self) -> list[dict]:
        """Turn on overflow slots and send pickers headed into the hot aisle to the cheaper copy."""
        self.overflow_enabled = True
        moved = []
        occ = self.occupancy()
        for a in self.agents:
            if a.state == "to_bin" and a.order and self.floor.aisle_of(a.order.slot) == HOT_AISLE:
                node, path, _ = self.slot_for(a.order.sku, a.node, occ)
                if node != a.order.slot:
                    a.order.slot, a.path, a.leg = node, path, 1
                    occ[HOT_AISLE] = max(0, occ.get(HOT_AISLE, 0) - 1)
                    occ[OVERFLOW_AISLE] = occ.get(OVERFLOW_AISLE, 0) + 1
                    moved.append({"picker": a.id, "order": a.order.id, "to": node})
        return moved

    # ------------------------------------------------------------------ reporting
    def counts(self) -> dict:
        c = Counter(o.status for o in self.orders.values())
        late = sum(1 for o in self.orders.values() if o.status != "shipped" and self.now > o.due)
        return {"queued": c["queued"], "picking": c["picking"], "packing": c["packing"], "shipped": c["shipped"],
                "late": late}

    def on_time_rate(self, since: float = 0.0) -> float | None:
        done = [o for o in self.orders.values() if o.status == "shipped" and o.created >= since]
        return sum(o.shipped_at <= o.due for o in done) / len(done) if done else None

    def aisle_stats(self, window: float = 600.0) -> list[dict]:
        occ = self.occupancy()
        recent = [(aisle, s) for t, aisle, s in self.pick_log if t >= self.now - window]
        waiting = Counter(self.floor.aisle_of(o.slot or bin_node(o.sku.aisle, o.sku.bay))
                          for o in self.orders.values() if o.status in ("queued", "picking") and self.now > o.due)
        out = []
        for i, letter in enumerate(AISLES):
            times = [s for a, s in recent if a == i]
            out.append({"aisle": letter, "people": occ.get(i, 0), "capacity": AISLE_CAPACITY,
                        "avg_pick_s": round(sum(times) / len(times), 1) if times else None,
                        "late_orders": waiting.get(i, 0)})
        return out

    def snapshot(self) -> dict:
        return {
            "t": round(self.now, 1),
            "agents": [{"id": a.id, "kind": a.kind, "x": round(a.x, 2), "y": round(a.y, 2), "state": a.state,
                        "order": a.order.id if a.order else None} for a in self.agents],
            "occupancy": self.occupancy(),
            "counts": self.counts(),
            "overflow": self.overflow_enabled,
            "orders": [self.order_view(o) for o in sorted(self.orders.values(), key=lambda o: -int(o.id))[:30]],
        }

    def order_view(self, o: Order) -> dict:
        node = o.slot or bin_node(o.sku.aisle, o.sku.bay)
        aisle, bay = AISLES.index(node[0]), int(node[1:])
        status = "late" if o.status in ("queued", "picking") and self.now > o.due else o.status
        return {"id": o.id, "item": o.sku.item, "sku": o.sku.sku, "aisle": AISLES[aisle], "bay": bay,
                "level": o.sku.level, "side": o.sku.side, "status": status, "picker": o.picker,
                "due_in_s": round(o.due - self.now)}
