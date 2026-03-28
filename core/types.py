from __future__ import annotations

from enum import Enum


class BotMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class AdaptationMode(str, Enum):
    NORMAL = "NORMAL"
    ACCEL = "ACCEL"
    BRAKE = "BRAKE"
