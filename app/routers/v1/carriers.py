"""`GET /v1/carriers` - a published carrier-coverage reference.

Client-requested: developers had no way to know upfront which carrier
prefixes are supported before spending a lookup on one that's very likely
to come back `no_data`. Backed by `app/core/carrier_coverage.py`'s curated,
hand-maintained tables - see that module's docstring for why this is
best-effort rather than a live-verified registry.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.carrier_coverage import KNOWN_UNSUPPORTED_PREFIXES, SUPPORTED_CARRIER_PREFIXES
from ...dependencies import ApiKeyPrincipal, get_api_key_principal
from ...schemas.carrier import CarrierCoverageOut, SupportedCarrierOut

router = APIRouter(prefix="/v1/carriers", tags=["carriers"])


@router.get("", response_model=CarrierCoverageOut)
def get_carrier_coverage(principal: ApiKeyPrincipal = Depends(get_api_key_principal)):
    return CarrierCoverageOut(
        supported=[
            SupportedCarrierOut(prefix=prefix, carrier=carrier)
            for prefix, carrier in sorted(SUPPORTED_CARRIER_PREFIXES.items())
        ],
        known_unsupported=sorted(KNOWN_UNSUPPORTED_PREFIXES),
    )
