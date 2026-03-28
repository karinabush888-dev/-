from __future__ import annotations

from core.models import Position


def exposure_of(p: Position) -> float:
    return abs(p.qty * p.avg_price)
