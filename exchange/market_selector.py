from __future__ import annotations

from core.models import Market, Outcome


def _liq_score(outcome: Outcome) -> float:
    return outcome.volume


def select_outcome(market: Market, prob_min: float, prob_max: float) -> Outcome:
    candidates = [o for o in market.outcomes if prob_min <= o.implied_prob <= prob_max]
    if not candidates:
        candidates = market.outcomes
    candidates.sort(key=lambda o: (_liq_score(o), -abs(0.5 - o.implied_prob)), reverse=True)
    return candidates[0]
