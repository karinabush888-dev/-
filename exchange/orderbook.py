from __future__ import annotations

from core.models import OrderBook


def spread_cents(book: OrderBook) -> float:
    return max(0.0, (book.best_ask - book.best_bid) * 100)


def quote_half_spread(book: OrderBook) -> float:
    return max(0.02, 0.5 * spread_cents(book) / 100)
