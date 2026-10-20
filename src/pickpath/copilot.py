"""Ops copilot: plain-English questions about the floor, answered from live state.

Same rule as my fraud project: **the language model routes, the code answers.**
Rules recognise the common questions instantly; an optional LLM (Gemini's
free tier works) only maps unusual phrasing onto the same fixed intents, and
its output is validated before use. Every number in an answer comes from the
simulation, never from the model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request

from .floor import AISLES, DOCK_XY, bin_node
from .nav import steps
from .sim import HOT_AISLE, OVERFLOW_AISLE, Warehouse

log = logging.getLogger(__name__)


def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9%'\-\s.]", " ", text.lower().replace("’", "'"))
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def parse_intent(text: str) -> dict:
    t = _norm(text)
    order = (re.search(r"\b(\d{4})\b", t) or [None, None])[1]
    if re.search(r"\b(re-?route|rebalance|fix it|fix that|fix the|move pickers|send pickers|unblock|use overflow)\b", t):
        return {"intent": "reroute"}
    if order:
        return {"intent": "locate", "order": order}
    agent = re.search(r"\b(robot|picker|r|p)[ -]?(\d{1,2})\b", t)
    if agent:
        return {"intent": "agent", "kind": "R" if agent[1][0] == "r" else "P", "n": int(agent[2])}
    if re.search(r"\b(late|delay|delayed|slow|behind|bottleneck|congest\w*|stuck|jam|why)\b", t):
        return {"intent": "diagnose"}
    if re.search(r"\b(status|summary|how many|orders|overview|report|progress)\b", t):
        return {"intent": "status"}
    return {"intent": "unknown"}


ROUTER_PROMPT = """You route questions for a warehouse operations dashboard.
Reply with ONLY one JSON object. Allowed shapes:
{"intent":"locate","order":"4417"}  {"intent":"diagnose"}  {"intent":"reroute"}
{"intent":"agent","kind":"P"|"R","n":3}  {"intent":"status"}  {"intent":"unknown"}
Question: """


class GeminiRouter:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest", opener=None, timeout: float = 6.0):
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.opener = opener or urllib.request.urlopen
        self.cache: dict[str, dict] = {}

    @classmethod
    def from_env(cls):
        key = os.environ.get("GEMINI_API_KEY")
        return cls(key, os.environ.get("GEMINI_MODEL", "gemini-flash-latest")) if key else None

    def route(self, text: str) -> dict:
        k = _norm(text)
        if k not in self.cache:
            body = json.dumps({"contents": [{"parts": [{"text": ROUTER_PROMPT + text[:500]}]}],
                               "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}).encode()
            req = urllib.request.Request(self.URL.format(model=self.model, key=self.api_key), data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with self.opener(req, timeout=self.timeout) as r:
                    raw = json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]
                self.cache[k] = validate(json.loads(raw))
            except Exception as e:
                log.warning("LLM routing failed: %s", e)
                self.cache[k] = {"intent": "unknown"}
        return self.cache[k]


def validate(d: dict) -> dict:
    i = d.get("intent")
    if i == "locate" and re.fullmatch(r"\d{4}", str(d.get("order", ""))):
        return {"intent": "locate", "order": str(d["order"])}
    if i == "agent" and d.get("kind") in ("P", "R") and str(d.get("n", "")).isdigit():
        return {"intent": "agent", "kind": d["kind"], "n": int(d["n"])}
    if i in ("diagnose", "reroute", "status"):
        return {"intent": i}
    return {"intent": "unknown"}


def navigation(wh: Warehouse, order_id: str) -> dict | None:
    """Route from the packing desk to an order's bin, with turn-by-turn steps."""
    o = wh.orders.get(order_id)
    if not o:
        return None
    node = o.slot or wh.slot_for(o.sku, "DOCK")[0]
    path, _ = wh.floor.route("DOCK", node, wh.occupancy())
    pts = wh.floor.polyline(path)
    aisle, bay = node[0], int(node[1:])
    return {"order": wh.order_view(o), "path": [list(p) for p in pts], "length_m": round(wh.floor.length(path), 1),
            "steps": steps(pts, aisle_letter=aisle, bay=bay, level=o.sku.level, side=o.sku.side),
            "origin": list(DOCK_XY)}


class Copilot:
    def __init__(self, wh: Warehouse, router: GeminiRouter | None = None):
        self.wh, self.router = wh, router

    def ask(self, text: str) -> dict:
        intent, by = parse_intent(text), "rules"
        if intent["intent"] == "unknown" and self.router:
            intent, by = self.router.route(text), "llm"
        return {"intent": intent, "routed_by": by, **getattr(self, "_" + intent["intent"])(intent)}

    def _locate(self, it):
        nav = navigation(self.wh, it["order"])
        if not nav:
            ids = sorted(self.wh.orders, key=int)
            return {"text": f"There's no order {it['order']} on this wave. Open orders run from #{ids[0]} to #{ids[-1]}."}
        o = nav["order"]
        shipped = o["status"] == "shipped"
        turns = sum(s["turn"] in ("left", "right") for s in nav["steps"])
        minutes = nav["length_m"] / 1.3 / 60
        prefix = f"Order {o['id']} already shipped, but here's where its item lives. " if shipped else ""
        return {"text": prefix + f"Order {o['id']}, {o['item']}: aisle {o['aisle']}, bay {o['bay']}, level {o['level']}, on your "
                        f"{o['side']}. {nav['length_m']:.0f} m from the packing desk with {turns} turns, about "
                        f"{max(1, round(minutes * 60))} seconds on foot.",
                "reasons": [s["text"] for s in nav["steps"]], "nav": nav}

    def _diagnose(self, _):
        stats = self.wh.aisle_stats()
        worst = max(stats, key=lambda s: (s["late_orders"], s["people"] - s["capacity"], s["avg_pick_s"] or 0))
        late = self.wh.counts()["late"]
        normal = [s["avg_pick_s"] for s in stats if s["avg_pick_s"] and s["people"] <= s["capacity"]]
        base = sum(normal) / len(normal) if normal else 12.0
        if worst["people"] <= worst["capacity"] and late == 0:
            return {"text": f"Nothing is congested right now. Average pick takes {base:.0f} s and every order is on time."}
        reasons = [f"Aisle {worst['aisle']} has {worst['people']} people in space for {worst['capacity']}."]
        if worst["avg_pick_s"]:
            reasons.append(f"Picks there average {worst['avg_pick_s']:.0f} s, against {base:.0f} s elsewhere "
                           f"({worst['avg_pick_s'] / base:.1f}× slower).")
        if late:
            reasons.append(f"{worst['late_orders']} of the {late} late orders are waiting on stock in aisle {worst['aisle']}.")
        else:
            reasons.append("No orders are late yet, but the queue for this aisle is growing.")
        if worst["aisle"] != AISLES[HOT_AISLE]:
            fix = (f"Aisle {worst['aisle']} has no overflow stock, so the fix is timing: it clears as these picks "
                   "finish. Holding new picks for it for a minute would help.")
        elif not self.wh.overflow_enabled:
            fix = (f"Aisle {AISLES[OVERFLOW_AISLE]} holds overflow copies of aisle {AISLES[HOT_AISLE]}'s best sellers. "
                   "Say “reroute pickers” to send pickers to whichever copy is faster to reach.")
        else:
            fix = "Overflow routing is already on; the backlog should clear shortly."
        return {"text": f"Aisle {worst['aisle']} is the bottleneck.", "reasons": reasons, "next_step": fix,
                "highlight": {"aisle": worst["aisle"]}}

    def _reroute(self, _):
        if self.wh.overflow_enabled:
            return {"text": "Overflow routing is already on. New picks go to whichever copy is faster to reach."}
        moved = self.wh.enable_overflow()
        who = ", ".join(m["picker"] for m in moved) or "no pickers mid-route"
        return {"text": f"Done. Overflow routing is on, and I redirected {who} to aisle {AISLES[OVERFLOW_AISLE]}. "
                        "New picks for those items go to whichever copy is faster to reach.", "moved": moved}

    def _agent(self, it):
        a = next((x for x in self.wh.agents if x.kind == it["kind"] and int(x.id[1:]) == it["n"]), None)
        if not a:
            p = sum(x.kind == "P" for x in self.wh.agents)
            r = sum(x.kind == "R" for x in self.wh.agents)
            return {"text": f"There's no {'robot R' if it['kind'] == 'R' else 'picker P'}{it['n']}. This shift has "
                            f"pickers P1–P{p} and robots R1–R{r}."}
        aisle = self.wh._aisle_at(a)
        where = (f"in aisle {AISLES[aisle]} near bay {min(12, max(1, int((a.y - 7) // 2) + 1))}" if aisle is not None
                 else "near the packing area" if a.y > 33 else "in a cross aisle")
        doing = {"picking": "picking", "to_bin": "walking to a pick", "to_dock": "carrying a pick to packing",
                 "dropping": "dropping off at packing", "idle": "waiting for work", "moving": "moving totes"}[a.state]
        extra = f" for order {a.order.id}" if a.order else ""
        return {"text": f"{'Robot' if a.kind == 'R' else 'Picker'} {a.id} is {where}, {doing}{extra}.",
                "highlight": {"agent": a.id}}

    def _status(self, _):
        c = self.wh.counts()
        rate = self.wh.on_time_rate()
        text = (f"{sum(v for k, v in c.items() if k != 'late')} orders this wave: {c['queued']} queued, {c['picking']} "
                f"picking, {c['packing']} packing, {c['shipped']} shipped. {c['late']} are late.")
        if rate is not None:
            text += f" On-time so far: {rate * 100:.0f}%."
        return {"text": text}

    def _unknown(self, _):
        hint = "" if self.router else " (Set GEMINI_API_KEY to also understand free-form questions.)"
        return {"text": "I didn't catch that. Try “where is order 4417?”, “why are orders late?”, “reroute pickers” "
                        "or “where is picker P3?”." + hint}


__all__ = ["Copilot", "GeminiRouter", "navigation", "parse_intent", "bin_node"]
