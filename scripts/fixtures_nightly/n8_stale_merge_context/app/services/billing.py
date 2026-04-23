from __future__ import annotations


def calculate_charge(plan: str, seats: int, weekend: bool = False) -> int:
    per_seat = {"starter": 1000, "pro": 1800, "enterprise": 3000}[plan]
    total = per_seat * seats
    if weekend:
        total += 500
    return total
