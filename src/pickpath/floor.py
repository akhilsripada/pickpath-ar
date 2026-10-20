"""Warehouse floor as a weighted graph, with congestion-aware A* routing.

Layout (metres): 8 aisles A-H, 12 bays each, a cross aisle at the top and the
bottom, and the packing dock below the bottom cross aisle.

    A   B   C   D   E   F   G   H
    T---T---T---T---T---T---T---T      <- top cross aisle (y=4)
    1   1   1   1   1   1   1   1
    :   :   :   :   :   :   :   :      <- bays 1..12, 2 m apart
    12  12  12  12  12  12  12  12
    B---B---B---B-J-B---B---B---B      <- bottom cross aisle (y=32), J = dock junction
                  |
                DOCK                    <- packing and dock (y=37.5)

An aisle fits two people working side by side. Every extra person in an
aisle makes walking and picking in it slower, so the router charges aisle
edges more as they fill up. It can't avoid the aisle a bin is in, but it
picks the cheaper of two entrances and, with overflow slots, the cheaper
of two copies of the same item.
"""
from __future__ import annotations

import heapq
import math

AISLES = "ABCDEFGH"
NBAY = 12
BAY0, BAYH = 7.0, 2.0
TOP, BOT = 4.0, 32.0
DOCK_XY = (32.0, 37.5)
AISLE_CAPACITY = 2
CONGESTION_PENALTY = 1.5      # extra cost per person over capacity, as a multiple of edge length


def aisle_x(i: int) -> float:
    return 7.0 + 7.0 * i


def bay_y(b: int) -> float:
    return BAY0 + (b - 0.5) * BAYH


def bin_node(aisle: int, bay: int) -> str:
    return f"{AISLES[aisle]}{bay}"


class Floor:
    def __init__(self):
        self.xy: dict[str, tuple[float, float]] = {}
        self.adj: dict[str, list[tuple[str, float, int | None]]] = {}
        for i, a in enumerate(AISLES):
            x = aisle_x(i)
            self.xy[a + "T"] = (x, TOP)
            self.xy[a + "B"] = (x, BOT)
            for b in range(1, NBAY + 1):
                self.xy[f"{a}{b}"] = (x, bay_y(b))
        self.xy["J"] = (DOCK_XY[0], BOT)
        self.xy["DOCK"] = DOCK_XY
        for n in self.xy:
            self.adj[n] = []
        for i, a in enumerate(AISLES):           # aisle chains carry their aisle index
            chain = [a + "T"] + [f"{a}{b}" for b in range(1, NBAY + 1)] + [a + "B"]
            for u, v in zip(chain, chain[1:]):
                self._edge(u, v, i)
        top = [a + "T" for a in AISLES]
        bottom = [a + "B" for a in AISLES[:4]] + ["J"] + [a + "B" for a in AISLES[4:]]
        for row in (top, bottom):
            for u, v in zip(row, row[1:]):
                self._edge(u, v, None)
        self._edge("J", "DOCK", None)

    def _edge(self, u: str, v: str, aisle: int | None):
        d = math.dist(self.xy[u], self.xy[v])
        self.adj[u].append((v, d, aisle))
        self.adj[v].append((u, d, aisle))

    def aisle_of(self, node: str) -> int | None:
        if node in ("J", "DOCK"):
            return None
        return AISLES.index(node[0]) if node[1:].isdigit() else None

    def route(self, start: str, goal: str, occupancy: dict[int, int] | None = None) -> tuple[list[str], float]:
        """A* shortest path. `occupancy` maps aisle index -> people in it (for congestion cost)."""
        occupancy = occupancy or {}
        gx, gy = self.xy[goal]
        h = lambda n: math.hypot(self.xy[n][0] - gx, self.xy[n][1] - gy)  # admissible: never over-estimates
        best = {start: 0.0}
        came: dict[str, str] = {}
        frontier = [(h(start), 0.0, start)]
        while frontier:
            _, g, u = heapq.heappop(frontier)
            if u == goal:
                path = [u]
                while u in came:
                    u = came[u]
                    path.append(u)
                return path[::-1], g
            if g > best.get(u, math.inf):
                continue
            for v, d, aisle in self.adj[u]:
                cost = d
                if aisle is not None:
                    over = max(0, occupancy.get(aisle, 0) - AISLE_CAPACITY)
                    cost *= 1 + CONGESTION_PENALTY * over
                ng = g + cost
                if ng < best.get(v, math.inf):
                    best[v], came[v] = ng, u
                    heapq.heappush(frontier, (ng + h(v), ng, v))
        raise ValueError(f"no route from {start} to {goal}")

    def length(self, path: list[str]) -> float:
        return sum(math.dist(self.xy[u], self.xy[v]) for u, v in zip(path, path[1:]))

    def polyline(self, path: list[str]) -> list[tuple[float, float]]:
        """Corner points only (collinear points merged)."""
        pts = [self.xy[n] for n in path]
        out = pts[:1]
        for p, q in zip(pts[1:], pts[2:] + [None]):
            if q is None:
                out.append(p)
                continue
            a = out[-1]
            if abs((p[0] - a[0]) * (q[1] - p[1]) - (p[1] - a[1]) * (q[0] - p[0])) > 1e-9:
                out.append(p)
        return out
