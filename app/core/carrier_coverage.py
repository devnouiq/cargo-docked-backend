"""Best-effort, hand-maintained carrier coverage reference.

Carrier support in this product is entirely live/dynamic - GoComet's own
`suggest-carrier` endpoint (providers/gocomet_http.py's `suggest_carrier`)
decides at request time which carrier (if any) a container number maps to,
and there is no offline database of ISO 6346 owner-prefix -> carrier
mappings anywhere upstream. These two tables are NOT a live-verified,
exhaustive registry - they're a curated snapshot seeded from carriers this
product already names elsewhere (registry.py, marketing copy) plus prefixes
a client explicitly confirmed return 0% data during testing. Exposed via
`GET /v1/carriers` (routers/v1/carriers.py) so a developer has *something*
concrete to check before spending a lookup on a number that's unlikely to
resolve - update both tables as new gaps are confirmed, don't treat this as
authoritative or complete.
"""

from __future__ import annotations

# Owner prefix (first 3-4 letters of the container number) -> carrier name,
# for carriers with a confirmed-working resolution path today (either via
# GoComet's auto-suggest or, for ROMU, Romeu's own dedicated API).
SUPPORTED_CARRIER_PREFIXES: dict[str, str] = {
    "MAEU": "Maersk",
    "MSCU": "MSC",
    "CMDU": "CMA CGM",
    "COSU": "COSCO",
    "HLCU": "Hapag-Lloyd",
    "ONEY": "ONE (Ocean Network Express)",
    "EGLV": "Evergreen",
    "ROMU": "Romeu Shipping",
}

# Prefixes empirically confirmed (via client-reported testing) to return
# 0% data through GoComet's auto-suggest - not necessarily because the
# carrier itself is unsupported, but because GoComet has no mapping for
# these specific owner codes today.
#
# NOTE: the client's report named these 5 explicitly plus "~40 other
# prefixes" it did not enumerate - this set currently only contains the
# named ones. Extend it as the remaining ~40 are confirmed/shared; don't
# treat this as the complete list.
KNOWN_UNSUPPORTED_PREFIXES: frozenset[str] = frozenset(
    {
        "MSBU", "MSPU", "MSYU", "MSZU", "MSGU",
    }
)


def is_known_unsupported(container_number: str) -> bool:
    prefix = container_number.strip().upper()[:4]
    return prefix in KNOWN_UNSUPPORTED_PREFIXES
