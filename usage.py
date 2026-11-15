"""What a map has cost Hermes to work on, counted per map from what Hermes tells a plugin.

Hermes hands a plugin two things this needs: every model call's token counts as it finishes (the
``post_api_request`` hook) and the end of each turn (``post_llm_call``). The map tool says which map a turn
worked on. A turn's calls are added up and, when the turn ends, written against that map: how many turns and
model calls, the tokens, the seconds, and what Hermes itself estimates the calls cost from the provider's
price list. That figure is Hermes's estimate, not an invoice.

Nothing here calls out anywhere, and no prompt, reply or tool result is read or kept: only the counts. No
Hermes imports at module level, so the tests run without Hermes.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from decimal import Decimal
from typing import Any, Callable, Optional

from . import store as st

log = logging.getLogger("hermes.plugins.stoa")
TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def hermes_price(model: Any, provider: Any, base_url: Any, usage: dict) -> Optional[int]:
    """What Hermes estimates one model call cost, in millionths of a dollar. None where it has no price for the model."""
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        names = {f.name for f in dataclasses.fields(CanonicalUsage)} - {"raw_usage"}
        counted = CanonicalUsage(**{k: int(v or 0) for k, v in usage.items() if k in names})
        result = estimate_usage_cost(str(model or ""), counted, provider=str(provider or "") or None, base_url=str(base_url or "") or None)
        if result.amount_usd is None or str(getattr(result.status, "value", result.status)) == "unknown":
            return None
        return int(Decimal(str(result.amount_usd)) * 1_000_000)
    except Exception:
        return None


def key(market_id: str, name: str) -> str:
    return "map:%s:%s" % (market_id, name)


class Meter:
    """Adds up the model calls of each turn in hand, and settles a turn against the maps it worked on."""

    def __init__(self, price: Callable = hermes_price):
        self._price = price
        self._turns: dict = {}
        self._lock = threading.Lock()

    @staticmethod
    def _blank(turn_id: str) -> dict:
        return dict({name: 0 for name in TOKENS}, turn=turn_id, calls=0, secs=0.0, cost=0, unpriced=0, maps={})

    def touch(self, session_id: Any, store: Any, market_id: str) -> None:
        """The turn in hand in this conversation is working on this map (called by the map tool)."""
        session_id = str(session_id or "")
        if not session_id or not market_id:
            return
        with self._lock:
            self._turns.setdefault(session_id, self._blank(""))["maps"][market_id] = store

    def on_api(self, **event: Any) -> None:
        """One model call finished: add its tokens, seconds and estimated cost to its turn."""
        session_id, usage = str(event.get("session_id") or ""), event.get("usage")
        if not session_id or not isinstance(usage, dict):
            return
        turn_id, left_over = str(event.get("turn_id") or ""), None
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is not None and turn["turn"] and turn_id and turn["turn"] != turn_id:
                left_over = self._turns.pop(session_id)      # the turn before never ended cleanly: settle what it did
                turn = None
            if turn is None:
                turn = self._turns[session_id] = self._blank(turn_id)
            turn["turn"] = turn["turn"] or turn_id
            turn["calls"] += 1
            for name in TOKENS:
                turn[name] += int(usage.get(name) or 0)
            try:
                turn["secs"] += float(event.get("api_duration") or 0)
            except (TypeError, ValueError):
                pass
            cost = self._price(event.get("model"), event.get("provider"), event.get("base_url"), usage)
            if cost is None:
                turn["unpriced"] += 1
            else:
                turn["cost"] += cost
        if left_over:
            self._settle(left_over)

    def on_turn_end(self, **event: Any) -> None:
        with self._lock:
            turn = self._turns.pop(str(event.get("session_id") or ""), None)
        if turn:
            self._settle(turn)

    @staticmethod
    def _settle(turn: dict) -> None:
        """Write a finished turn against the maps it worked on, shared equally when it worked on more than one."""
        maps = turn["maps"]
        if not maps or not turn["calls"]:
            return
        share, month, now = len(maps), st.today()[:7], st.now()
        for market_id, store in maps.items():
            try:
                if not store.meta_get(key(market_id, "since")):
                    store.meta_set(key(market_id, "since"), now)
                store.meta_set(key(market_id, "last"), now)
                store.bump(key(market_id, "runs"), 1)
                store.bump(key(market_id, "calls"), max(1, turn["calls"] // share))
                for name in TOKENS:
                    store.bump(key(market_id, name), turn[name] // share)
                store.bump(key(market_id, "secs"), int(round(turn["secs"] / share)))
                store.bump(key(market_id, "unpriced"), turn["unpriced"] // share)
                store.bump(key(market_id, "cost"), turn["cost"] // share)
                store.bump(key(market_id, "cost:" + month), turn["cost"] // share)
            except Exception as exc:      # counting never gets in the way of the work
                log.warning("stoa: a turn's cost could not be written against map %s: %s", market_id, exc)
