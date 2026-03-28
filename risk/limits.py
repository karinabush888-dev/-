from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DynamicSizing:
    mm_allocation: float
    mis_allocation: float
    order_size_mm: float
    order_size_mis: float
    max_exposure_per_outcome: float
