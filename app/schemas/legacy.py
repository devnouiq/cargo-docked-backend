"""Pre-rework request/response models - unchanged, moved verbatim from the
old top-level app/schemas.py. Still used by app/routers/searates_debug.py
(untouched behavior) and the deprecated `/v1/track*` aliases in
app/routers/tracking.py.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class TrackRequest(BaseModel):
    container_numbers: List[str]


class TrackResponse(BaseModel):
    container_number: str
    status: str
    location: Optional[str]
    raw_data: Optional[dict]
    cached: bool = False
    duration_seconds: Optional[float] = None
    # Additive (both optional, default None) - this deprecated response
    # shape originally had no equivalent of the standardized API's
    # tracking_status/tracking_message fields at all, meaning a client
    # reading either key here always got a missing key (which most JSON
    # tooling reports as null with no further explanation) rather than a
    # real value. See routers/tracking.py's _to_track_response().
    tracking_status: Optional[str] = None
    tracking_message: Optional[str] = None


class BulkTrackRequest(BaseModel):
    container_numbers: List[str]
    batch_size: int = 10
    sealine: str = "AUTO"
