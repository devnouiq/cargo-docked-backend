"""Best-effort, hand-maintained carrier coverage reference.

Carrier support in this product is entirely live/dynamic - GoComet's own
`suggest-carrier` endpoint (providers/gocomet_http.py's `suggest_carrier`)
decides at request time which carrier (if any) a container number maps to,
and there is no offline database of ISO 6346 owner-prefix -> carrier
mappings anywhere upstream. These two tables are NOT a live-verified,
exhaustive registry - they're a curated snapshot seeded from carriers this
product already names elsewhere (registry.py, marketing copy), prefixes a
client explicitly confirmed return 0% data during testing, and (as of
2026-09-18) every prefix seen in a live 2,000-container stress test run
against `GET /v1/track-searates/{number}?sealine=AUTO` covering all 20
carriers the client's own report named. Exposed via `GET /v1/carriers`
(routers/v1/carriers.py) so a developer has *something* concrete to check
before spending a lookup on a number that's unlikely to resolve - update
both tables as new gaps are confirmed, don't treat this as authoritative
or complete.
"""

from __future__ import annotations

# Owner prefix (first 3-4 letters of the container number) -> carrier name,
# for carriers with a confirmed-working resolution path today (either via
# GoComet's auto-suggest or, for ROMU, Romeu's own dedicated API).
#
# The block below the original hand-seeded entries was confirmed live on
# 2026-09-18: every prefix here resolved to a carrier (found or not found
# is a separate question - this only means GoComet's auto-suggest knows
# what carrier the prefix belongs to) in at least 90% of its observed
# sample within that run.
SUPPORTED_CARRIER_PREFIXES: dict[str, str] = {
    "MAEU": "Maersk",
    "MSCU": "MSC",
    "CMDU": "CMA CGM",
    "COSU": "COSCO",
    "HLCU": "Hapag-Lloyd",
    "ONEY": "ONE (Ocean Network Express)",
    "EGLV": "Evergreen",
    "ROMU": "Romeu Shipping",
    # --- confirmed via the 2026-09-18 2,000-container stress test ---
    "ARKU": "Arkas",
    "APHU": "CMA CGM",
    "APRU": "CMA CGM",
    "APZU": "CMA CGM",
    "CGMU": "CMA CGM",
    "CMAU": "CMA CGM",
    "CBHU": "COSCO",
    "CSLU": "COSCO",
    "CSNU": "COSCO",
    "EGHU": "Evergreen",
    "EGSU": "Evergreen",
    "EISU": "Evergreen",
    "EITU": "Evergreen",
    "EMCU": "Evergreen",
    "HMCU": "Evergreen",
    "HDMU": "HMM",
    "FANU": "Hapag-Lloyd",
    "HAMU": "Hapag-Lloyd",
    "HLBU": "Hapag-Lloyd",
    "HLXU": "Hapag-Lloyd",
    "NIDU": "Hapag-Lloyd",
    "UACU": "Hapag-Lloyd",
    "UAEU": "Hapag-Lloyd",
    "UASU": "Hapag-Lloyd",
    "KMTU": "KMTC",
    "MEDU": "MSC",
    "MSDU": "MSC",
    "MSMU": "MSC",
    "MSNU": "MSC",
    "HASU": "Maersk",
    "MIEU": "Maersk",
    "MMAU": "Maersk",
    "MRKU": "Maersk",
    "MRSU": "Maersk",
    "PONU": "Maersk",
    "KKFU": "ONE",
    "KKTU": "ONE",
    "MOAU": "ONE",
    "MOFU": "ONE",
    "MORU": "ONE",
    "MOTU": "ONE",
    "NYKU": "ONE",
    "ONEU": "ONE",
    "PCIU": "PIL",
    "PIDU": "PIL",
    "SMCU": "SM Line",
    "WHLU": "Wan Hai",
    "WHSU": "Wan Hai",
    "YMLU": "Yang Ming",
    "YMMU": "Yang Ming",
}

# Prefixes empirically confirmed to return 0% data through GoComet's
# auto-suggest - not necessarily because the carrier itself is unsupported,
# but because GoComet has no mapping for these specific owner codes today.
#
# The first five were named directly in the client's original report ("~40
# other prefixes" were mentioned but not enumerated at the time). The block
# below them is the full set confirmed live on 2026-09-18, one 2,000-
# container stress test (100 containers per carrier, all 20 carriers the
# client's report named) run through GET /v1/track-searates/{number}
# ?sealine=AUTO - every prefix below returned a 422 carrier-not-resolved
# for 100% of its sample. Six carriers (Emirates, KMTC's few exceptions
# aside, Matson, OOCL, SITC, Sinokor, ZIM) had *no* prefix in that test
# resolve at all - see SUPPORTED_CARRIER_PREFIXES above for what did.
KNOWN_UNSUPPORTED_PREFIXES: frozenset[str] = frozenset(
    {
        "MSBU", "MSPU", "MSYU", "MSZU", "MSGU",
        # --- confirmed via the 2026-09-18 2,000-container stress test ---
        "AMCU", "ANNU", "APLU", "CMNU", "CSFU", "CSOU", "CZLU", "DVRU",
        "KLCU", "MMCU", "NOLU", "NOSU", "OPDU", "OTAU", "SMUU", "STMU",
        "CSGU", "CVTU", "OERU",
        "ESDU", "ESPU",
        "UGMU",
        "CASU", "CMUU", "CPSU", "CSQU", "CSVU", "DAYU", "ITAU", "IVLU",
        "LNXU", "NDSU", "QIBU", "QNNU", "TLEU", "TMMU",
        "GTIU",
        "APMU", "CADU", "CNIU", "COZU", "ENAU", "FAAU", "FRLU", "GRIU",
        "KNLU", "LOTU", "MALU", "MCAU", "MCHU", "MCPU", "MHHU", "MRFU",
        "MSAU", "MSFU", "MSWU", "MVIU", "MWMU", "OCLU", "POCU", "SCMU",
        "SEAU", "SUDU", "TORU",
        "CXCU", "HRZU", "MATU",
        "AKLU", "KLFU", "KLTU", "MOEU", "MOGU", "MOSU",
        "OOCU", "OOLU",
        "PILU",
        "CWFU", "SITU", "SWAU",
        "SKHU", "SKLU", "SKOU", "SKRU",
        "TPCU",
        "ZCLU", "ZCSU", "ZIMU", "ZMOU",
    }
)


def is_known_unsupported(container_number: str) -> bool:
    prefix = container_number.strip().upper()[:4]
    return prefix in KNOWN_UNSUPPORTED_PREFIXES
