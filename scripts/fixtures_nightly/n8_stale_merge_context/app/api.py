from __future__ import annotations

from fastapi import APIRouter

from app.services.billing import calculate_charge

router = APIRouter()


@router.get("/quote")
def quote(plan: str, seats: int, weekend: bool = False) -> dict[str, int]:
    return {"amount_cents": calculate_charge(plan, seats, weekend=weekend)}
