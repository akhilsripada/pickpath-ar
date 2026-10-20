# Benchmark results

`python bench.py --hours 1 --seeds 10` on CPython 3.13.16, x86_64. Each run simulates 1 h of a
wave (7 pickers, 3 robots, one order every 12 s on average, best sellers ordered 2.5x as often and slotted in aisle F).

## What overflow routing buys

| | Overflow off | Overflow on |
|---|---|---|
| Orders shipped on time (mean of 10 runs) | **86.2%** | **99.7%** |
| Worst run | 35.1% | 96.8% |
| Orders shipped per run | 300 | 296 |
| Median order cycle time | 173 s | 113 s |
| 95th-percentile cycle time | 315 s | 206 s |

## Speed

| | |
|---|---|
| Congestion-aware A* route (any bin to any bin, 99 nodes) | **44 µs** per query |
| One simulation step (10 agents, routing, orders) | **22 µs** |
