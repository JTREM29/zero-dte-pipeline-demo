from __future__ import annotations
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy


def test_crossover_emits_enter_and_exit():
    strat = SimpleIntradaySPXStrategy(fast=2, slow=4)
    prices = [10, 11, 12, 13, 12, 11, 10, 11, 12]
    events = []
    for p in prices:
        for sig in strat.evaluate({"lastPrice": p}):
            if sig.name in {"enter_long", "exit_long"}:
                events.append(sig.name)
    # We expect at least one enter and one exit
    assert "enter_long" in events
    assert "exit_long" in events