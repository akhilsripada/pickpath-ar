"""Turn-by-turn directions from a route polyline, for the AR view and voice."""
from __future__ import annotations

import math


def steps(points: list[tuple[float, float]], *, aisle_letter: str, bay: int, level: int, side: str) -> list[dict]:
    """Return [{text, turn, distance, start, end}] for a corner-only polyline.

    `turn` is the manoeuvre at the END of the step: "left", "right" or "arrive".
    Screen/floor coordinates have y pointing down, so a positive cross product
    of the two headings is a right turn.
    """
    if len(points) < 2:
        return [{"text": f"You're here. Bay {aisle_letter}-{bay}, level {level}, on your {side}.",
                 "turn": "arrive", "distance": 0.0, "start": points[0], "end": points[0]}]
    out = []
    segs = list(zip(points, points[1:]))
    for i, (a, b) in enumerate(segs):
        d = math.dist(a, b)
        if i == len(segs) - 1:
            turn = "arrive"
        else:
            c = segs[i + 1][1]
            h1, h2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
            cross = h1[0] * h2[1] - h1[1] * h2[0]
            turn = "right" if cross > 0 else "left"
        if i == 0 and len(segs) > 1:
            text = f"Walk {round(d)} m to the main aisle"
        elif turn == "arrive":
            text = f"Walk {round(d)} m. Bay {aisle_letter}-{bay} is on your {side}, level {level}"
        else:
            text = f"Walk {round(d)} m"
        if turn in ("left", "right"):
            into = f" into aisle {aisle_letter}" if i == len(segs) - 2 else ""
            text += f", then turn {turn}{into}"
        out.append({"text": text, "turn": turn, "distance": round(d, 2), "start": a, "end": b})
    return out
