from __future__ import annotations

from pydantic import BaseModel, Field


class SupportedCarrierOut(BaseModel):
    prefix: str
    carrier: str


class CarrierCoverageOut(BaseModel):
    supported: list[SupportedCarrierOut]
    known_unsupported: list[str]
    note: str = Field(
        default=(
            "Best-effort/empirical coverage reference, not a live-verified or "
            "exhaustive list. `supported` prefixes have a confirmed working "
            "resolution path today; `known_unsupported` prefixes have been "
            "empirically confirmed to return no data. A prefix appearing in "
            "neither list has simply not been characterized yet - it may "
            "still resolve via the carrier's live auto-suggest."
        )
    )
