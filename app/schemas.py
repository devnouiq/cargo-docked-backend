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


class BulkTrackRequest(BaseModel):
    container_numbers: List[str]
    batch_size: int = 10
    sealine: str = "AUTO"
