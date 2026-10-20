"""Measure what overflow routing buys, and how fast routing and simulation run.

    python bench.py                    # 10 seeds x 1 simulated hour each (~10 s)
    python bench.py --hours 2 --seeds 20

Writes docs/results.md. Standard library only.
"""
from __future__ import annotations

import argparse
import platform
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from pickpath.floor import AISLES, NBAY, Floor, bin_node  # noqa: E402
from pickpath.sim import Warehouse  # noqa: E402


def wave(seed: int, overflow: bool, hours: float) -> dict:
    w = Warehouse(seed=seed, overflow=overflow)
    w.run(hours * 3600)
    shipped = [o for o in w.orders.values() if o.status == "shipped"]
    cycle = [o.shipped_at - o.created for o in shipped]
    return {"on_time": w.on_time_rate(), "shipped": len(shipped),
            "p50_cycle": statistics.median(cycle) if cycle else 0, "p95_cycle": sorted(cycle)[int(0.95 * (len(cycle) - 1))] if cycle else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--seeds", type=int, default=10)
    args = ap.parse_args()

    rows = {}
    for overflow in (False, True):
        runs = [wave(s, overflow, args.hours) for s in range(args.seeds)]
        rows[overflow] = {k: statistics.mean(r[k] for r in runs) for k in runs[0]}
        rows[overflow]["worst_on_time"] = min(r["on_time"] for r in runs)

    floor, rng = Floor(), random.Random(1)
    nodes = [bin_node(a, b) for a in range(len(AISLES)) for b in range(1, NBAY + 1)] + ["DOCK"]
    pairs = [(rng.choice(nodes), rng.choice(nodes)) for _ in range(20000)]
    occ = {5: 4, 2: 3}
    t0 = time.perf_counter()
    for a, b in pairs:
        floor.route(a, b, occ)
    route_us = (time.perf_counter() - t0) / len(pairs) * 1e6

    w = Warehouse(seed=3)
    t0 = time.perf_counter()
    steps = 4000
    for _ in range(steps):
        w.step(0.1)
    step_us = (time.perf_counter() - t0) / steps * 1e6

    env = f"{platform.python_implementation()} {platform.python_version()}, {platform.machine()}"
    off, on = rows[False], rows[True]
    md = f"""# Benchmark results

`python bench.py --hours {args.hours:g} --seeds {args.seeds}` on {env}. Each run simulates {args.hours:g} h of a
wave (7 pickers, 3 robots, one order every 12 s on average, best sellers ordered 2.5x as often and slotted in aisle F).

## What overflow routing buys

| | Overflow off | Overflow on |
|---|---|---|
| Orders shipped on time (mean of {args.seeds} runs) | **{off['on_time'] * 100:.1f}%** | **{on['on_time'] * 100:.1f}%** |
| Worst run | {off['worst_on_time'] * 100:.1f}% | {on['worst_on_time'] * 100:.1f}% |
| Orders shipped per run | {off['shipped']:.0f} | {on['shipped']:.0f} |
| Median order cycle time | {off['p50_cycle']:.0f} s | {on['p50_cycle']:.0f} s |
| 95th-percentile cycle time | {off['p95_cycle']:.0f} s | {on['p95_cycle']:.0f} s |

## Speed

| | |
|---|---|
| Congestion-aware A* route (any bin to any bin, {len(floor.xy)} nodes) | **{route_us:.0f} µs** per query |
| One simulation step (10 agents, routing, orders) | **{step_us:.0f} µs** |
"""
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "results.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
