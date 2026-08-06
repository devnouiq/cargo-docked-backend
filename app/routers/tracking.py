import asyncio
import logging
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..dependencies import get_api_key
from ..providers.track_trace_browser import scrape_container
from ..schemas import TrackRequest, TrackResponse
from ..services.tracking_service import get_or_track

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["tracking"])


@router.post("/track", response_model=List[TrackResponse])
async def track_containers(request: TrackRequest, customer: models.Customer = Depends(get_api_key), db: Session = Depends(get_db)):
    batch_start = time.perf_counter()

    if customer.requests_today + len(request.container_numbers) > customer.daily_limit:
        raise HTTPException(status_code=429, detail="Daily request limit exceeded")

    customer.requests_today += len(request.container_numbers)
    db.commit()

    results = await asyncio.gather(*[
        get_or_track(db, number, scrape_container) for number in request.container_numbers
    ])

    batch_elapsed = time.perf_counter() - batch_start
    logger.info(f"/v1/track batch of {len(request.container_numbers)} completed in {batch_elapsed:.2f}s total")

    return [TrackResponse(**r) for r in results]


@router.get("/track/{number}", response_model=TrackResponse)
async def track_single(number: str, customer: models.Customer = Depends(get_api_key), db: Session = Depends(get_db)):
    request_start = time.perf_counter()
    if customer.requests_today + 1 > customer.daily_limit:
        raise HTTPException(status_code=429, detail="Daily request limit exceeded")

    customer.requests_today += 1
    db.commit()

    result = await get_or_track(db, number, scrape_container)

    logger.info(f"/v1/track/{number} completed in {time.perf_counter() - request_start:.3f}s (cached={result['cached']})")
    return TrackResponse(**result)


@router.get("/providers")
async def get_providers(customer: models.Customer = Depends(get_api_key)):
    # This would typically be a dynamic list or queried from a DB table
    # For now, returning a static list of embedded providers
    return {
        "providers": [
            "MSC",
            "Maersk",
            "CMA CGM",
            "Hapag-Lloyd",
            "ONE",
            "Evergreen"
        ]
    }
